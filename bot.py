"""
Telegram-бот для мониторинга новостей F1.
Обрабатывает inline-кнопки, генерацию и публикацию постов.
"""

import asyncio
import html
import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Optional

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHANNEL_ID,
    HYPE_THRESHOLD,
    CHECK_INTERVAL_MINUTES,
)
from scraper import NewsItem, collect_new_news, fetch_article_content, clear_seen, load_seen
from analyzer import analyze_news_batch, generate_news_post, find_related_post
from storage import (
    add_published,
    get_recent_posts,
    get_recent_posts_for_context,
    find_post_by_uid,
    load_published,
    load_daily_cache,
    save_daily_cache,
    remove_posts_by_msg_ids,
)

logger = logging.getLogger(__name__)

# Хранилище для сгенерированных постов и данных новостей
# Ключ — uid новости, значение — dict с данными
news_cache: dict[str, dict] = {}
generated_posts: dict[str, str] = {}
# Хранилище для текста, который пользователь редактирует
editing_state: dict[int, str] = {}  # chat_id -> uid
# Хранилище для прикреплённых фото (uid -> file_id)
post_photos: dict[str, str] = {}
# Состояние ожидания фото от пользователя (chat_id -> uid)
photo_state: dict[int, str] = {}
# Выбранный reply-target (uid новости -> channel_message_id)
reply_targets: dict[str, int] = {}
# Дневной кэш ВСЕХ проанализированных новостей (дата -> список dict)
# Хранит новости за текущий день для команды /digest, сохраняется в файл
daily_news_cache: dict[str, list[dict]] = load_daily_cache()
# Chat ID владельца — запоминается при первом /start
# Сохраняется в файл для переживания рестартов
OWNER_CHAT_ID_FILE = Path(__file__).parent / "owner_chat_id.json"
owner_chat_id: Optional[int] = None


def _load_owner_chat_id() -> Optional[int]:
    """Загрузить owner_chat_id из файла."""
    if OWNER_CHAT_ID_FILE.exists():
        try:
            data = json.loads(OWNER_CHAT_ID_FILE.read_text())
            chat_id = data.get("owner_chat_id")
            if chat_id is not None:
                logger.info(f"owner_chat_id загружен из файла: {chat_id}")
                return int(chat_id)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Ошибка чтения owner_chat_id: {e}")
    return None


def _save_owner_chat_id(chat_id: int) -> None:
    """Сохранить owner_chat_id в файл."""
    OWNER_CHAT_ID_FILE.write_text(json.dumps({"owner_chat_id": chat_id}))
    logger.info(f"owner_chat_id сохранён в файл: {chat_id}")


async def _cleanup_deleted_posts(bot) -> list[dict]:
    """Проверить, существуют ли посты в канале. Удалить удалённые. Вернуть живые."""
    posts = get_recent_posts(50)
    if not posts:
        return []

    deleted_ids = set()
    for p in posts:
        msg_id = p.get("channel_message_id")
        if not msg_id:
            continue
        try:
            # copyMessage с from → to (owner), затем удаляем копию
            # Это самый надёжный способ проверить существование поста
            copied = await bot.copy_message(
                chat_id=owner_chat_id,
                from_chat_id=TELEGRAM_CHANNEL_ID,
                message_id=msg_id,
            )
            # Сразу удалить скопированное сообщение
            try:
                await bot.delete_message(chat_id=owner_chat_id, message_id=copied.message_id)
            except Exception:
                pass
        except Exception:
            # Пост удалён или недоступен
            deleted_ids.add(msg_id)
            logger.info(f"Пост msg_id={msg_id} удалён из канала — убираю из хранилища")

    if deleted_ids:
        remove_posts_by_msg_ids(deleted_ids)
        posts = [p for p in posts if p.get("channel_message_id") not in deleted_ids]

    return posts


def markdown_to_html(text: str) -> str:
    """Конвертировать Markdown-форматирование в HTML для Telegram."""
    import re
    # **bold** -> <b>bold</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # *italic* -> <i>italic</i>
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
    # `code` -> <code>code</code>
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    return text


def hype_emoji(score: int) -> str:
    """Эмодзи в зависимости от оценки хайпа."""
    if score >= 9:
        return "🔥🔥🔥"
    elif score >= 8:
        return "🔥🔥"
    elif score >= 7:
        return "🔥"
    return "📰"


def format_news_alert(item: NewsItem) -> str:
    """Форматировать новость для отправки пользователю."""
    emoji = hype_emoji(item.hype_score)
    text = (
        f"{emoji} <b>Хайп: {item.hype_score}/10</b>\n\n"
        f"<b>{html.escape(item.summary)}</b>\n\n"
        f"📌 Источник: {html.escape(item.source)}\n"
        f"🔗 <a href=\"{item.url}\">Читать оригинал</a>"
    )
    return text


def news_alert_keyboard(uid: str) -> InlineKeyboardMarkup:
    """Клавиатура для новости — кнопка генерации."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Сгенерировать новость", callback_data=f"generate:{uid}")]
    ])


def generated_post_keyboard(uid: str) -> InlineKeyboardMarkup:
    """Клавиатура для сгенерированного поста."""
    has_photo = uid in post_photos
    has_reply = uid in reply_targets
    photo_label = "🖼 Картинка ✅" if has_photo else "🖼 Картинка"
    reply_label = "↩️ Reply ✅" if has_reply else "↩️ Reply"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 Отправить в канал", callback_data=f"publish:{uid}"),
        ],
        [
            InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit:{uid}"),
            InlineKeyboardButton("🔄 Перегенерировать", callback_data=f"regenerate:{uid}"),
        ],
        [
            InlineKeyboardButton(photo_label, callback_data=f"photo:{uid}"),
            InlineKeyboardButton(reply_label, callback_data=f"replyselect:{uid}"),
        ],
    ])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start."""
    global owner_chat_id
    owner_chat_id = update.message.chat_id
    _save_owner_chat_id(owner_chat_id)
    logger.info(f"Owner chat_id сохранён: {owner_chat_id}")

    await update.message.reply_text(
        "🏎️ <b>F1 News Bot</b>\n\n"
        "Я мониторю новостные сайты о Формуле 1 и присылаю тебе самые горячие новости.\n\n"
        "Команды:\n"
        "/start — Приветствие\n"
        "/check — Проверить новости прямо сейчас\n"
        "/digest — Показать новости с хайпом 3-7 за сегодня\n"
        "/status — Статус бота\n"
        "/sethype &lt;число&gt; — Изменить порог хайпа (текущий: {threshold})\n\n"
        "Бот автоматически проверяет новости каждые {interval} минут.".format(
            threshold=HYPE_THRESHOLD,
            interval=CHECK_INTERVAL_MINUTES,
        ),
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /status."""
    await update.message.reply_text(
        f"✅ Бот работает\n"
        f"📊 Порог хайпа: {HYPE_THRESHOLD}/10\n"
        f"⏱ Интервал проверки: {CHECK_INTERVAL_MINUTES} мин\n"
        f"📰 Новостей в кэше: {len(news_cache)}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручная проверка новостей по команде /check."""
    msg = await update.message.reply_text("⏳ Собираю новости...")
    
    try:
        news = collect_new_news()
        if not news:
            await msg.edit_text("✅ Новых новостей не найдено.")
            return

        await msg.edit_text(f"🔍 Найдено {len(news)} новостей. Анализирую...")

        # Анализ пачками по 10
        analyzed = []
        for i in range(0, len(news), 10):
            batch = news[i:i + 10]
            batch = await analyze_news_batch(batch)
            analyzed.extend(batch)

        # Сохранить ВСЕ проанализированные новости в дневной кэш
        _save_to_daily_cache(analyzed)

        # Отфильтровать по хайпу
        hot_news = [n for n in analyzed if n.hype_score >= HYPE_THRESHOLD]
        hot_news.sort(key=lambda x: x.hype_score, reverse=True)

        if not hot_news:
            await msg.edit_text(
                f"📊 Проанализировано {len(analyzed)} новостей.\n"
                f"Новостей с хайпом ≥ {HYPE_THRESHOLD} не найдено."
            )
            return

        await msg.edit_text(
            f"📊 Проанализировано {len(analyzed)} новостей.\n"
            f"🔥 Горячих новостей: {len(hot_news)}"
        )

        # Отправить каждую горячую новость
        for item in hot_news:
            news_cache[item.uid] = {
                "title": item.title,
                "url": item.url,
                "source": item.source,
                "summary": item.summary,
                "hype_score": item.hype_score,
            }
            await update.message.chat.send_message(
                text=format_news_alert(item),
                parse_mode=ParseMode.HTML,
                reply_markup=news_alert_keyboard(item.uid),
                disable_web_page_preview=True,
            )
            await asyncio.sleep(0.5)  # Не спамить

    except Exception as e:
        logger.error(f"Ошибка при проверке новостей: {e}", exc_info=True)
        await msg.edit_text(f"❌ Ошибка: {str(e)[:200]}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий inline-кнопок."""
    query = update.callback_query
    await query.answer()

    data = query.data
    parts = data.split(":", 2)  # макс. 3 части (action:uid:extra)
    action = parts[0]
    uid = parts[1] if len(parts) > 1 else ""
    extra = parts[2] if len(parts) > 2 else ""

    if action == "generate":
        await handle_generate(query, uid)
    elif action == "regenerate":
        await handle_generate(query, uid, is_regen=True)
    elif action == "publish":
        await handle_publish(query, uid, context)
    elif action == "edit":
        await handle_edit(query, uid, context)
    elif action == "photo":
        await handle_photo_request(query, uid, context)
    elif action == "replyselect":
        page = int(extra) if extra.isdigit() else 0
        await handle_reply_select(query, uid, context.bot, page)
    elif action == "replypick":
        await handle_reply_pick(query, uid)
    elif action == "replyclear":
        await handle_reply_clear(query, uid)
    elif action == "confirmreply":
        # extra = channel_message_id
        try:
            msg_id = int(extra)
            reply_targets[uid] = msg_id
            await _do_publish(query, uid, generated_posts[uid], msg_id, context)
        except (ValueError, KeyError) as e:
            await query.message.reply_text(f"❌ Ошибка: {e}")
    elif action == "clearseen":
        if uid == "confirm":
            count = clear_seen()
            await query.edit_message_text(f"✅ Бакет очищен — удалено {count} записей.")
        else:
            await query.edit_message_text("👌 Отменено.")
        return
    elif action == "publishnow":
        # Публиковать без reply
        if uid in generated_posts:
            await _do_publish(query, uid, generated_posts[uid], None, context)
        else:
            await query.message.reply_text("⚠️ Пост не найден.")


async def handle_generate(query, uid: str, is_regen: bool = False):
    """Генерация поста по новости."""
    if uid not in news_cache:
        await query.message.reply_text("⚠️ Новость не найдена в кэше. Попробуйте /check заново.")
        return

    news_data = news_cache[uid]
    status_msg = await query.message.reply_text(
        "⏳ Получаю текст статьи и генерирую пост..." if not is_regen
        else "🔄 Перегенерирую пост..."
    )

    try:
        # Получить полный текст статьи
        article_content = fetch_article_content(news_data["url"])
        if not article_content:
            article_content = f"{news_data['title']}\n{news_data.get('summary', '')}"

        # Загрузить последние посты канала для контекста
        previous_posts = get_recent_posts_for_context(7)

        # Генерация через ChatGPT
        post = await generate_news_post(
            title=news_data["title"],
            url=news_data["url"],
            article_content=article_content,
            previous_posts=previous_posts if previous_posts else None,
        )

        # Сохранить в кэш
        generated_posts[uid] = post

        await status_msg.edit_text(
            f"📝 <b>Сгенерированный пост:</b>\n\n{post}",
            parse_mode=ParseMode.HTML,
            reply_markup=generated_post_keyboard(uid),
            disable_web_page_preview=False,
        )

    except Exception as e:
        logger.error(f"Ошибка генерации: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Ошибка генерации: {str(e)[:200]}")


async def handle_publish(query, uid: str, context: ContextTypes.DEFAULT_TYPE):
    """Отправить пост в канал (с фото если прикреплено)."""
    if uid not in generated_posts:
        await query.message.reply_text("⚠️ Пост не найден. Сгенерируйте заново.")
        return

    post = generated_posts[uid]
    reply_msg_id = reply_targets.get(uid)  # None если не выбран reply

    # Если reply не выбран вручную — попробовать найти автоматически
    if reply_msg_id is None:
        await query.message.reply_text("⏳ Проверяю связь с предыдущими постами...")
        published = await _cleanup_deleted_posts(context.bot)
        if published:
            news_data = news_cache.get(uid, {})
            related_uid = await find_related_post(
                new_post_title=news_data.get("title", ""),
                new_post_text=post,
                published_posts=published,
            )
            if related_uid:
                related = find_post_by_uid(related_uid)
                if related:
                    # Предложить пользователю
                    related_title = related.get("title", "Без заголовка")[:60]
                    related_msg_id = related.get("channel_message_id")
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            f"✅ Да, reply на «{related_title}»",
                            callback_data=f"confirmreply:{uid}:{related_msg_id}",
                        )],
                        [InlineKeyboardButton(
                            "❌ Нет, без reply",
                            callback_data=f"publishnow:{uid}",
                        )],
                        [InlineKeyboardButton(
                            "↩️ Выбрать другой пост",
                            callback_data=f"replyselect:{uid}",
                        )],
                    ])
                    await query.message.reply_text(
                        f"🔗 Найден связанный пост:\n\n"
                        f"<b>«{html.escape(related_title)}»</b>\n\n"
                        f"Опубликовать как ответ на него?",
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                    )
                    return

    # Публикуем
    await _do_publish(query, uid, post, reply_msg_id, context)


async def _do_publish(
    query,
    uid: str,
    post: str,
    reply_msg_id: int | None,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Фактическая отправка поста в канал."""
    try:
        send_kwargs = {}
        if reply_msg_id:
            send_kwargs["reply_to_message_id"] = reply_msg_id

        if uid in post_photos:
            msg = await context.bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo=post_photos[uid],
                caption=post[:1024],
                parse_mode=ParseMode.HTML,
                **send_kwargs,
            )
        else:
            msg = await context.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=post,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
                **send_kwargs,
            )

        # Сохранить в историю опубликованных постов
        news_data = news_cache.get(uid, {})
        add_published(
            uid=uid,
            title=news_data.get("title", "Без заголовка"),
            text=post,
            channel_message_id=msg.message_id,
        )

        # Очистить reply-target
        reply_targets.pop(uid, None)

        reply_info = ""
        if reply_msg_id:
            reply_info = " (↩️ reply)"
        await query.message.reply_text(f"✅ Пост успешно отправлен в канал!{reply_info}")

    except Exception as e:
        logger.error(f"Ошибка публикации: {e}", exc_info=True)
        await query.message.reply_text(f"❌ Ошибка публикации: {str(e)[:200]}")


async def handle_edit(query, uid: str, context: ContextTypes.DEFAULT_TYPE):
    """Запустить режим редактирования."""
    if uid not in generated_posts:
        await query.message.reply_text("⚠️ Пост не найден. Сгенерируйте заново.")
        return

    chat_id = query.message.chat_id
    editing_state[chat_id] = uid

    await query.message.reply_text(
        "✏️ Скопируйте пост ниже, отредактируйте и отправьте мне.\n"
        "/cancel — отмена",
    )
    await query.message.reply_text(
        generated_posts[uid],
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def handle_photo_request(query, uid: str, context: ContextTypes.DEFAULT_TYPE):
    """Запросить фото для поста."""
    if uid not in generated_posts:
        await query.message.reply_text("⚠️ Пост не найден. Сгенерируйте заново.")
        return

    chat_id = query.message.chat_id

    if uid in post_photos:
        # Фото уже есть — предложить заменить или удалить
        await query.message.reply_text(
            "🖼 К посту уже прикреплено фото.\n\n"
            "Отправьте новое фото чтобы заменить, или /cancel для отмены.",
        )
    else:
        await query.message.reply_text(
            "🖼 Отправьте мне фото для этого поста.\n\n"
            "Или отправьте /cancel для отмены.",
        )

    photo_state[chat_id] = uid


async def handle_reply_select(query, uid: str, bot, page: int = 0):
    """Показать список постов для выбора reply (по 5 штук, новые сверху)."""
    published = await _cleanup_deleted_posts(bot)
    if not published:
        await query.message.reply_text("📭 Нет опубликованных постов для reply.")
        return

    PAGE_SIZE = 5
    # Сортировка: новые первые
    published_desc = list(reversed(published))
    total = len(published_desc)
    start = page * PAGE_SIZE
    page_posts = published_desc[start : start + PAGE_SIZE]

    if not page_posts:
        await query.message.reply_text("📭 Больше постов нет.")
        return

    buttons = []
    for p in page_posts:
        title = p.get("title", "Без заголовка")[:45]
        msg_id = p.get("channel_message_id", 0)
        buttons.append([InlineKeyboardButton(
            f"📌 {title}",
            callback_data=f"replypick:{uid}:{msg_id}",
        )])

    # Кнопка "Ещё 5" если есть следующая страница
    if start + PAGE_SIZE < total:
        buttons.append([InlineKeyboardButton(
            "➡️ Ещё 5...",
            callback_data=f"replyselect:{uid}:{page + 1}",
        )])

    # Кнопка "Без reply"
    buttons.append([InlineKeyboardButton(
        "❌ Без reply",
        callback_data=f"replyclear:{uid}",
    )])

    text = f"↩️ <b>Выберите пост для reply</b> (стр. {page + 1}):"
    if page == 0:
        await query.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:
        # Обновить существующее сообщение вместо нового
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def handle_reply_pick(query, uid: str):
    """Обработка выбора конкретного поста для reply.
    
    uid в данном случае содержит news_uid, а extra (3-я часть) — msg_id.
    Но из handle_callback extra уже разобрана — тут uid = 'news_uid' out of 'replypick:news_uid:msg_id'.
    Нужно получить msg_id из callback_data напрямую.
    """
    # callback_data = "replypick:news_uid:channel_msg_id"
    parts = query.data.split(":", 2)
    if len(parts) < 3:
        await query.message.reply_text("❌ Ошибка выбора.")
        return

    news_uid = parts[1]
    try:
        msg_id = int(parts[2])
    except ValueError:
        await query.message.reply_text("❌ Ошибка: некорректный ID поста.")
        return

    reply_targets[news_uid] = msg_id
    await query.message.reply_text(
        f"✅ Reply установлен! (msg_id: {msg_id})\n\n"
        "Нажмите «📤 Отправить в канал» для публикации.",
        reply_markup=generated_post_keyboard(news_uid),
    )


async def handle_reply_clear(query, uid: str):
    """Очистить выбранный reply."""
    reply_targets.pop(uid, None)
    await query.message.reply_text(
        "✅ Reply убран.\n\n"
        "Нажмите «📤 Отправить в канал» для публикации.",
        reply_markup=generated_post_keyboard(uid),
    )


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка входящих фото (для прикрепления к посту)."""
    chat_id = update.message.chat_id

    if chat_id not in photo_state:
        return

    uid = photo_state.pop(chat_id)

    # Берём фото наибольшего размера
    photo = update.message.photo[-1]
    file_id = photo.file_id

    post_photos[uid] = file_id

    await update.message.reply_text(
        "✅ Фото прикреплено к посту!\n\n"
        f"📝 <b>Пост с фото готов к публикации.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=generated_post_keyboard(uid),
    )


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений (для редактирования)."""
    chat_id = update.message.chat_id

    # Если ждём фото, но пришёл текст — отмена
    if chat_id in photo_state:
        uid = photo_state.pop(chat_id)
        await update.message.reply_text("❌ Ожидалось фото. Прикрепление отменено.")
        return

    if chat_id in editing_state:
        uid = editing_state.pop(chat_id)
        new_text = update.message.text_html or update.message.text or ""
        new_text = new_text.strip()

        if new_text.startswith("/"):
            await update.message.reply_text("❌ Редактирование отменено.")
            return

        generated_posts[uid] = new_text

        await update.message.reply_text(
            f"✅ Пост обновлён!\n\n📝 <b>Новый вариант:</b>\n\n{new_text}",
            parse_mode=ParseMode.HTML,
            reply_markup=generated_post_keyboard(uid),
            disable_web_page_preview=True,
        )


def _save_to_daily_cache(items: list[NewsItem]):
    """Сохранить все проанализированные новости в дневной кэш."""
    today = date.today().isoformat()
    # Очистить кэш за прошлые дни
    old_keys = [k for k in daily_news_cache if k != today]
    for k in old_keys:
        del daily_news_cache[k]

    if today not in daily_news_cache:
        daily_news_cache[today] = []

    existing_uids = {item["uid"] for item in daily_news_cache[today]}
    for item in items:
        if item.uid not in existing_uids:
            daily_news_cache[today].append({
                "uid": item.uid,
                "title": item.title,
                "url": item.url,
                "source": item.source,
                "summary": item.summary,
                "hype_score": item.hype_score,
            })
            existing_uids.add(item.uid)

    # Сохранить в файл
    save_daily_cache(daily_news_cache)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очистить бакет обработанных новостей."""
    chat_id = update.effective_chat.id
    if owner_chat_id and chat_id != owner_chat_id:
        return

    count = len(load_seen())
    if count == 0:
        await update.message.reply_text("📭 Бакет уже пуст.")
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🗑 Да, удалить ({count} шт.)", callback_data="clearseen:confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data="clearseen:cancel"),
        ]
    ])
    await update.message.reply_text(
        f"⚠️ Очистить бакет обработанных новостей?\n"
        f"Сейчас в нём <b>{count}</b> записей.\n\n"
        f"После очистки бот заново найдёт все текущие новости.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /digest — показать новости с хайпом 3-6 за сегодня."""
    today = date.today().isoformat()
    today_news = daily_news_cache.get(today, [])

    # Фильтр: хайп от 3 до 7 (не попавшие в горячие, но не совсем мусор)
    medium_news = [n for n in today_news if 3 <= n["hype_score"] <= 7]
    medium_news.sort(key=lambda x: x["hype_score"], reverse=True)

    if not medium_news:
        await update.message.reply_text(
            f"📭 Новостей с хайпом 3-7 за сегодня не найдено.\n\n"
            f"Всего новостей в дневном кэше: {len(today_news)}\n"
            f"Попробуйте сначала /check чтобы собрать свежие новости."
        )
        return

    await update.message.reply_text(
        f"📋 Новости с хайпом 3-7 за сегодня: {len(medium_news)} шт."
    )

    for item_data in medium_news:
        uid = item_data["uid"]
        # Сохранить в news_cache для возможности генерации
        news_cache[uid] = item_data

        emoji = hype_emoji(item_data["hype_score"])
        text = (
            f"{emoji} <b>Хайп: {item_data['hype_score']}/10</b>\n\n"
            f"<b>{html.escape(item_data['summary'])}</b>\n\n"
            f"📌 Источник: {html.escape(item_data['source'])}\n"
            f"🔗 <a href=\"{item_data['url']}\">Читать оригинал</a>"
        )
        await update.message.chat.send_message(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=news_alert_keyboard(uid),
            disable_web_page_preview=True,
        )
        await asyncio.sleep(0.3)


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача — автоматическая проверка новостей."""
    logger.info("Запуск автоматической проверки новостей...")

    if owner_chat_id is None:
        logger.warning("owner_chat_id не задан. Отправьте /start боту.")
        return

    try:
        news = collect_new_news()
        if not news:
            logger.info("Новых новостей не найдено.")
            return

        # Анализ пачками
        analyzed = []
        for i in range(0, len(news), 10):
            batch = news[i:i + 10]
            batch = await analyze_news_batch(batch)
            analyzed.extend(batch)

        # Сохранить ВСЕ проанализированные новости в дневной кэш
        _save_to_daily_cache(analyzed)

        # Отфильтровать по хайпу
        hot_news = [n for n in analyzed if n.hype_score >= HYPE_THRESHOLD]
        hot_news.sort(key=lambda x: x.hype_score, reverse=True)

        if not hot_news:
            logger.info(f"Проанализировано {len(analyzed)} новостей, горячих нет.")
            return

        logger.info(f"Найдено {len(hot_news)} горячих новостей!")

        for item in hot_news:
            news_cache[item.uid] = {
                "title": item.title,
                "url": item.url,
                "source": item.source,
                "summary": item.summary,
                "hype_score": item.hype_score,
            }
            await context.bot.send_message(
                chat_id=owner_chat_id,
                text=format_news_alert(item),
                parse_mode=ParseMode.HTML,
                reply_markup=news_alert_keyboard(item.uid),
                disable_web_page_preview=True,
            )
            await asyncio.sleep(0.5)

    except Exception as e:
        logger.error(f"Ошибка автоматической проверки: {e}", exc_info=True)


async def post_init(application: Application):
    """Установить подсказки команд в меню бота."""
    await application.bot.set_my_commands([
        BotCommand("start", "Приветствие и справка"),
        BotCommand("check", "Проверить новости прямо сейчас"),
        BotCommand("digest", "Дайджест новостей (хайп 3-7) за сегодня"),
        BotCommand("status", "Статус бота"),
        BotCommand("clear", "Очистить бакет новостей"),
    ])
    logger.info("Меню команд установлено")


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перехватывать все посты канала (включая ручные) для истории."""
    msg = update.channel_post
    if not msg:
        return

    logger.info(f"Получен пост канала: chat_id={msg.chat_id}, msg_id={msg.message_id}")

    text = msg.text or msg.caption or ""
    if not text.strip():
        logger.info("Пост канала без текста — пропущен")
        return

    # Проверить что этот message_id ещё не сохранён (избежать дублей от ботовых постов)
    from storage import load_published
    existing = load_published()
    existing_msg_ids = {p.get("channel_message_id") for p in existing}
    if msg.message_id in existing_msg_ids:
        return

    # Извлечь заголовок — первая строка текста
    title = text.split("\n")[0][:80]
    # Убрать HTML-теги из заголовка
    import re
    title = re.sub(r"<[^>]+>", "", title).strip()

    add_published(
        uid=f"manual_{msg.message_id}",
        title=title or "Ручной пост",
        text=text,
        channel_message_id=msg.message_id,
    )
    logger.info(f"Сохранён пост канала: msg_id={msg.message_id}, title={title[:40]}")


def create_bot() -> Application:
    """Создать и настроить Telegram-бота."""
    global owner_chat_id
    owner_chat_id = _load_owner_chat_id()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("clear", cmd_clear))

    # Inline-кнопки
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Посты канала (сохраняем все, включая ручные) — ПЕРЕД photo/text чтобы не перехватывались
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))

    # Фото (для прикрепления к постам) — только личные сообщения
    app.add_handler(MessageHandler(
        filters.PHOTO & ~filters.UpdateType.CHANNEL_POST, handle_photo_message
    ))

    # Текстовые сообщения (для редактирования) — только личные сообщения
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.CHANNEL_POST, handle_text_message
    ))

    # Автоматическая проверка по расписанию
    job_queue = app.job_queue
    job_queue.run_repeating(
        scheduled_check,
        interval=CHECK_INTERVAL_MINUTES * 60,
        first=30,  # первая проверка через 30 секунд после старта
    )

    return app
