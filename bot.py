"""
Telegram-бот для мониторинга новостей F1.
Обрабатывает inline-кнопки, генерацию и публикацию постов.
"""

import asyncio
import html
import json
import logging
import os
import re
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
    MEME_HOT_SCORE,
    MEME_CHECK_INTERVAL_MINUTES,
    MEME_MAX_AGE_HOURS,
)
from scraper import NewsItem, collect_new_news, fetch_article_content
from analyzer import analyze_news_batch, generate_news_post, deduplicate_news, find_related_post, translate_meme_caption
from image_search import search_news_image, download_image
from meme_scraper import collect_new_memes, MemeItem, load_seen_memes, save_seen_memes, mark_meme_seen, mark_meme_published, clear_seen_memes
from storage import (
    add_published,
    get_recent_posts,
    get_recent_posts_for_context,
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
# Просмотренные через /digest uid (чистятся через /clear)
digest_seen: set[str] = set()
# --- Состояние поиска фото ---
# uid -> list[str] (найденные URL изображений)
image_search_results: dict[str, list[str]] = {}
# --- Состояние мемов ---
# chat_id -> list[MemeItem] (очередь мемов для просмотра)
meme_queue: dict[int, list[MemeItem]] = {}
# uid -> текущая подпись (может быть отредактирована/переведена)
meme_captions: dict[str, str] = {}
# uid -> оригинальная подпись (для кнопки "Оригинал")
meme_originals: dict[str, str] = {}
# chat_id -> uid (режим редактирования подписи мема)
meme_editing: dict[int, str] = {}
# Саммари уже отправленных сегодня горячих алертов (для дедупа по теме)
_sent_topics: list[str] = []
_sent_topics_date: str = ""
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

def _is_owner(chat_id: int) -> bool:
    """Проверить, является ли пользователь владельцем бота."""
    return owner_chat_id is not None and chat_id == owner_chat_id

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
            InlineKeyboardButton("🔍 Найти фото", callback_data=f"imgsearch:{uid}"),
        ],
        [
            InlineKeyboardButton(reply_label, callback_data=f"replyselect:{uid}"),
        ],
    ])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start."""
    global owner_chat_id
    chat_id = update.message.chat_id

    # Если owner уже задан и это не он — игнорировать
    if owner_chat_id is not None and chat_id != owner_chat_id:
        await update.message.reply_text("⛔ Этот бот приватный.")
        return

    owner_chat_id = chat_id
    _save_owner_chat_id(owner_chat_id)
    logger.info(f"Owner chat_id сохранён: {owner_chat_id}")

    await update.message.reply_text(
        "🏎️ <b>F1 News Bot</b>\n\n"
        "Я мониторю новостные сайты о Формуле 1 и присылаю тебе самые горячие новости.\n\n"
        "Команды:\n"
        "/start — Приветствие\n"
        "/check — Проверить новости прямо сейчас\n"
        "/digest — Показать новости с хайпом 3-7 за сегодня\n"
        "/memes — Свежие мемы из Reddit (r/formuladank)\n"
        "/status — Статус бота\n"
        "/clear — Скрыть просмотренный дайджест\n"
        "/clearmemes — Очистить просмотренные мемы\n\n"
        "Бот автоматически проверяет новости каждые {interval} минут.\n"
        "Горячие мемы (score ≥ {meme_hot}) присылаются автоматически.".format(
            interval=CHECK_INTERVAL_MINUTES,
            meme_hot=MEME_HOT_SCORE,
        ),
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /status."""
    if not _is_owner(update.effective_chat.id):
        return
    await update.message.reply_text(
        f"✅ Бот работает\n"
        f"📊 Порог хайпа: {HYPE_THRESHOLD}/10\n"
        f"⏱ Интервал проверки: {CHECK_INTERVAL_MINUTES} мин\n"
        f"📰 Новостей в кэше: {len(news_cache)}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручная проверка новостей по команде /check."""
    if not _is_owner(update.effective_chat.id):
        return
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

        # Дедупликация по теме
        hot_news = await _dedup_hot_news(hot_news)
        if not hot_news:
            await msg.edit_text(
                f"📊 Проанализировано {len(analyzed)} новостей.\n"
                f"Все горячие новости — дубликаты уже отправленных тем."
            )
            return

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
            _track_sent_topic(item.summary)
            await asyncio.sleep(0.5)  # Не спамить

    except Exception as e:
        logger.error(f"Ошибка при проверке новостей: {e}", exc_info=True)
        await msg.edit_text(f"❌ Ошибка: {str(e)[:200]}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий inline-кнопок."""
    query = update.callback_query
    if not _is_owner(query.from_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
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
    elif action == "imgsearch":
        await handle_image_search(query, uid)
    elif action == "imgpick":
        await handle_image_pick(query, uid, extra)
    elif action == "replyselect":
        page = int(extra) if extra.isdigit() else 0
        await handle_reply_select(query, uid, context.bot, page)
    elif action == "replypick":
        await handle_reply_pick(query, uid)
    elif action == "replyclear":
        await handle_reply_clear(query, uid)
    # --- Мемы ---
    elif action == "meme_publish":
        await handle_meme_publish(query, uid, context)
    elif action == "meme_edit":
        await handle_meme_edit(query, uid)
    elif action == "meme_translate":
        await handle_meme_translate(query, uid)
    elif action == "meme_original":
        await handle_meme_original(query, uid)
    elif action == "meme_next":
        await handle_meme_next(query, context)
    elif action == "meme_stop":
        await handle_meme_stop(query)


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

        # Извлечь заголовок из текста поста (первая строка, без HTML-тегов)
        first_line = post.split("\n")[0][:80]
        post_title = re.sub(r"<[^>]+>", "", first_line).strip() or "Без заголовка"

        # Сохранить в историю опубликованных постов
        add_published(
            uid=uid,
            title=post_title,
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


# ─── Поиск фото ────────────────────────────────────────────────────────

async def handle_image_search(query, uid: str):
    """Поиск изображений для поста."""
    if uid not in news_cache:
        await query.message.reply_text("⚠️ Новость не найдена в кэше.")
        return

    news_data = news_cache[uid]
    title = news_data.get("title", "")

    status_msg = await query.message.reply_text("🔍 Ищу фото...")

    try:
        urls = await search_news_image(title, count=5)
        if not urls:
            await status_msg.edit_text(
                "😕 Фото не найдены. Попробуйте прикрепить вручную через «🖼 Картинка»."
            )
            return

        image_search_results[uid] = urls

        # Отправить найденные фото как media group
        from telegram import InputMediaPhoto
        media = []
        for i, url in enumerate(urls):
            caption = f"📷 {i + 1}" if i == 0 else ""
            media.append(InputMediaPhoto(media=url, caption=f"📷 Фото {i + 1}"))

        try:
            await query.message.chat.send_media_group(media=media)
        except Exception as e:
            logger.warning(f"Не удалось отправить media group: {e}")
            # Fallback: отправить по одной
            for i, url in enumerate(urls):
                try:
                    await query.message.chat.send_photo(photo=url, caption=f"📷 Фото {i + 1}")
                except Exception:
                    pass

        # Кнопки выбора
        buttons = []
        row = []
        for i in range(len(urls)):
            row.append(InlineKeyboardButton(f"📷 {i + 1}", callback_data=f"imgpick:{uid}:{i}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data=f"imgpick:{uid}:cancel")])

        await status_msg.edit_text(
            "👆 Выберите фото:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    except Exception as e:
        logger.error(f"Ошибка поиска фото: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Ошибка поиска: {str(e)[:200]}")


async def handle_image_pick(query, uid: str, extra: str):
    """Обработка выбора фото из результатов поиска."""
    if extra == "cancel":
        await query.message.edit_text("❌ Выбор фото отменён.")
        return

    try:
        idx = int(extra)
    except (ValueError, TypeError):
        await query.message.reply_text("❌ Ошибка выбора.")
        return

    urls = image_search_results.get(uid, [])
    if idx < 0 or idx >= len(urls):
        await query.message.reply_text("❌ Фото не найдено.")
        return

    url = urls[idx]
    status_msg = await query.message.reply_text("⏳ Скачиваю фото...")

    try:
        image_data = await download_image(url)
        if not image_data:
            await status_msg.edit_text("❌ Не удалось скачать фото. Попробуйте другое.")
            return

        # Отправить фото в чат чтобы получить file_id от Telegram
        import io
        sent = await query.message.chat.send_photo(
            photo=io.BytesIO(image_data),
            caption="✅ Фото выбрано для поста",
        )
        file_id = sent.photo[-1].file_id
        post_photos[uid] = file_id

        # Очистить результаты поиска
        image_search_results.pop(uid, None)

        await status_msg.edit_text(
            "✅ Фото прикреплено к посту!",
            reply_markup=generated_post_keyboard(uid),
        )

    except Exception as e:
        logger.error(f"Ошибка прикрепления фото: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:200]}")


# ─── Мемы ──────────────────────────────────────────────────────────────

def meme_keyboard(uid: str, is_translated: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура для мема."""
    translate_btn = (
        InlineKeyboardButton("🔙 Оригинал", callback_data=f"meme_original:{uid}")
        if is_translated
        else InlineKeyboardButton("🌐 Перевести", callback_data=f"meme_translate:{uid}")
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать", callback_data=f"meme_publish:{uid}"),
            InlineKeyboardButton("✏️ Редактировать", callback_data=f"meme_edit:{uid}"),
        ],
        [
            translate_btn,
            InlineKeyboardButton("⏭ Следующий", callback_data=f"meme_next:{uid}"),
        ],
        [
            InlineKeyboardButton("🚫 Стоп", callback_data=f"meme_stop:{uid}"),
        ],
    ])


def _format_meme_caption(meme: MemeItem, custom_caption: str | None = None) -> str:
    """Форматирование подписи мема для превью."""
    caption = custom_caption or meme.title
    return (
        f"{caption}\n\n"
        f"� r/{meme.subreddit}\n"
        f"🔗 {meme.permalink}"
    )


async def _show_meme(chat, meme: MemeItem, is_translated: bool = False):
    """Показать один мем с клавиатурой."""
    caption = meme_captions.get(meme.uid, meme.title)
    display_caption = _format_meme_caption(meme, caption)

    # Ограничение Telegram на подпись фото — 1024 символа
    if len(display_caption) > 1024:
        display_caption = display_caption[:1020] + "..."

    try:
        await chat.send_photo(
            photo=meme.image_url,
            caption=display_caption,
            reply_markup=meme_keyboard(meme.uid, is_translated),
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить мем {meme.uid}: {e}")
        # Fallback: текстовое сообщение со ссылкой
        await chat.send_message(
            text=f"🖼 Не удалось загрузить картинку\n\n{display_caption}",
            reply_markup=meme_keyboard(meme.uid, is_translated),
            disable_web_page_preview=False,
        )

    # Пометить как просмотренный
    mark_meme_seen(meme.uid)


async def cmd_memes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /memes — показать свежие мемы из Reddit."""
    if not _is_owner(update.effective_chat.id):
        return

    msg = await update.message.reply_text("🔍 Ищу свежие мемы на Reddit...")

    try:
        memes = collect_new_memes()
        if not memes:
            await msg.edit_text("🤷‍♂️ Новых мемов нет. Попробуйте позже.")
            return

        chat_id = update.message.chat_id
        meme_queue[chat_id] = memes

        await msg.edit_text(f"😂 Найдено {len(memes)} новых мемов! Показываю...")

        # Показать первый мем
        meme = meme_queue[chat_id].pop(0)
        meme_captions[meme.uid] = meme.title
        meme_originals[meme.uid] = meme.title
        await _show_meme(update.message.chat, meme)

    except Exception as e:
        logger.error(f"Ошибка команды /memes: {e}", exc_info=True)
        await msg.edit_text(f"❌ Ошибка: {str(e)[:200]}")


async def cmd_clearmemes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очистить список просмотренных мемов."""
    if not _is_owner(update.effective_chat.id):
        return
    count = clear_seen_memes()
    await update.message.reply_text(
        f"✅ Очищено {count} просмотренных мемов.\n"
        f"При следующем /memes они снова появятся (если ещё свежие)."
    )


async def handle_meme_publish(query, uid: str, context: ContextTypes.DEFAULT_TYPE):
    """Опубликовать мем в канал."""
    caption = meme_captions.get(uid, "")
    if not caption:
        await query.message.reply_text("⚠️ Подпись мема не найдена.")
        return

    try:
        # Найти image_url — он в meme_originals context нет, ищем через seen или message
        # Берём фото из самого сообщения query
        photo = None
        if query.message.photo:
            photo = query.message.photo[-1].file_id
        elif query.message.caption:
            # Мем уже показан — берём его photo
            pass

        if photo:
            msg = await context.bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo=photo,
                caption=caption[:1024],
            )
        else:
            # Fallback — текстовый пост
            msg = await context.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=caption,
            )

        mark_meme_published(uid)

        # Сохранить в историю
        add_published(
            uid=f"meme_{uid}",
            title=caption[:80],
            text=caption,
            channel_message_id=msg.message_id,
        )

        await query.message.reply_text("✅ Мем опубликован в канал!")

    except Exception as e:
        logger.error(f"Ошибка публикации мема: {e}", exc_info=True)
        await query.message.reply_text(f"❌ Ошибка публикации: {str(e)[:200]}")


async def handle_meme_edit(query, uid: str):
    """Войти в режим редактирования подписи мема."""
    chat_id = query.message.chat_id
    meme_editing[chat_id] = uid

    current = meme_captions.get(uid, "")
    await query.message.reply_text(
        "✏️ Введите новую подпись для мема:\n\n"
        f"Текущая: <i>{html.escape(current[:200])}</i>\n\n"
        "/cancel — отмена",
        parse_mode=ParseMode.HTML,
    )


async def handle_meme_translate(query, uid: str):
    """Перевести подпись мема на русский через AI."""
    original = meme_originals.get(uid, meme_captions.get(uid, ""))
    if not original:
        await query.message.reply_text("⚠️ Подпись мема не найдена.")
        return

    status_msg = await query.message.reply_text("🌐 Перевожу...")

    try:
        translated = await translate_meme_caption(original)
        meme_captions[uid] = translated

        await status_msg.edit_text(
            f"🌐 <b>Перевод:</b>\n\n{html.escape(translated)}",
            parse_mode=ParseMode.HTML,
        )

        # Обновить кнопки — показать "Оригинал" вместо "Перевести"
        # Перепоказать мем-сообщение с новой подписью невозможно (фото),
        # но показываем кнопки для дальнейших действий
        await query.message.reply_text(
            "👆 Перевод готов. Что дальше?",
            reply_markup=meme_keyboard(uid, is_translated=True),
        )

    except Exception as e:
        logger.error(f"Ошибка перевода мема: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Ошибка перевода: {str(e)[:200]}")


async def handle_meme_original(query, uid: str):
    """Вернуть оригинальную подпись мема."""
    original = meme_originals.get(uid, "")
    if not original:
        await query.message.reply_text("⚠️ Оригинал не найден.")
        return

    meme_captions[uid] = original
    await query.message.reply_text(
        f"🔙 <b>Оригинал:</b>\n\n{html.escape(original)}",
        parse_mode=ParseMode.HTML,
        reply_markup=meme_keyboard(uid, is_translated=False),
    )


async def handle_meme_next(query, context: ContextTypes.DEFAULT_TYPE):
    """Показать следующий мем из очереди."""
    chat_id = query.message.chat_id
    queue = meme_queue.get(chat_id, [])

    if not queue:
        await query.message.reply_text("📭 Больше мемов нет. Попробуйте /memes позже.")
        return

    meme = queue.pop(0)
    meme_captions[meme.uid] = meme.title
    meme_originals[meme.uid] = meme.title
    await _show_meme(query.message.chat, meme)


async def handle_meme_stop(query):
    """Остановить просмотр мемов."""
    chat_id = query.message.chat_id
    remaining = len(meme_queue.get(chat_id, []))
    meme_queue.pop(chat_id, None)
    await query.message.reply_text(
        f"🛑 Просмотр мемов завершён.\n"
        f"Пропущено мемов в очереди: {remaining}"
    )


async def scheduled_meme_check(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая проверка новых мемов — уведомление если появились свежие."""
    if owner_chat_id is None:
        return

    try:
        new_memes = collect_new_memes()

        if not new_memes:
            return

        count = len(new_memes)
        logger.info(f"Найдено {count} новых мемов на Reddit")

        # Просто уведомляем что есть новые мемы, не спамим картинками
        await context.bot.send_message(
            chat_id=owner_chat_id,
            text=f"😂 <b>Новые мемы на Reddit!</b>\n\n"
                 f"Найдено {count} новых мемов на r/formuladank.\n"
                 f"Используй /memes чтобы посмотреть.",
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        logger.error(f"Ошибка проверки мемов: {e}", exc_info=True)


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка входящих фото (для прикрепления к посту)."""
    chat_id = update.message.chat_id
    if not _is_owner(chat_id):
        return

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
    """Обработка текстовых сообщений (для редактирования постов и мемов)."""
    chat_id = update.message.chat_id
    if not _is_owner(chat_id):
        return

    # Если ждём фото, но пришёл текст — отмена
    if chat_id in photo_state:
        uid = photo_state.pop(chat_id)
        await update.message.reply_text("❌ Ожидалось фото. Прикрепление отменено.")
        return

    # Редактирование подписи мема
    if chat_id in meme_editing:
        uid = meme_editing.pop(chat_id)
        new_text = update.message.text or ""
        new_text = new_text.strip()

        if new_text.startswith("/"):
            await update.message.reply_text("❌ Редактирование мема отменено.")
            return

        meme_captions[uid] = new_text
        # Определить — было ли отредактировано после перевода
        is_translated = (uid in meme_originals and meme_originals[uid] != new_text
                         and meme_captions.get(uid) != meme_originals.get(uid))

        await update.message.reply_text(
            f"✅ Подпись мема обновлена!\n\n<i>{html.escape(new_text[:300])}</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=meme_keyboard(uid, is_translated=is_translated),
        )
        return

    # Редактирование поста
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


def _track_sent_topic(summary: str):
    """Запомнить саммари отправленного алерта для дедупликации по теме."""
    global _sent_topics, _sent_topics_date
    today = date.today().isoformat()
    if _sent_topics_date != today:
        _sent_topics.clear()
        _sent_topics_date = today
    _sent_topics.append(summary)


async def _dedup_hot_news(hot_news: list[NewsItem]) -> list[NewsItem]:
    """Убрать из горячих новостей дубликаты уже отправленных тем."""
    global _sent_topics, _sent_topics_date
    today = date.today().isoformat()
    if _sent_topics_date != today:
        _sent_topics.clear()
        _sent_topics_date = today
    if _sent_topics:
        hot_news = await deduplicate_news(hot_news, _sent_topics)
    return hot_news


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
    """Пометить все текущие дайджест-новости как просмотренные."""
    if not _is_owner(update.effective_chat.id):
        return

    today = date.today().isoformat()
    today_news = daily_news_cache.get(today, [])
    medium = [n for n in today_news if 3 <= n["hype_score"] <= 7 and n["uid"] not in digest_seen]

    if not medium:
        await update.message.reply_text("📭 Нет непросмотренных дайджест-новостей.")
        return

    count = len(medium)
    for n in medium:
        digest_seen.add(n["uid"])

    await update.message.reply_text(
        f"✅ Отмечено <b>{count}</b> новостей как просмотренные.\n"
        f"При следующем /digest они не появятся.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /digest — показать новости с хайпом 3-6 за сегодня."""
    if not _is_owner(update.effective_chat.id):
        return
    today = date.today().isoformat()
    today_news = daily_news_cache.get(today, [])

    # Фильтр: хайп от 3 до 7, исключая просмотренные
    medium_news = [n for n in today_news if 3 <= n["hype_score"] <= 7 and n["uid"] not in digest_seen]
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

        # Дедупликация по теме — убрать новости на ту же тему, что уже отправлялись
        hot_news = await _dedup_hot_news(hot_news)
        if not hot_news:
            logger.info("Все горячие новости отфильтрованы как дубликаты тем.")
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
            _track_sent_topic(item.summary)
            await asyncio.sleep(0.5)

    except Exception as e:
        logger.error(f"Ошибка автоматической проверки: {e}", exc_info=True)


async def post_init(application: Application):
    """Установить подсказки команд в меню бота."""
    await application.bot.set_my_commands([
        BotCommand("start", "Приветствие и справка"),
        BotCommand("check", "Проверить новости прямо сейчас"),
        BotCommand("digest", "Дайджест новостей (хайп 3-7) за сегодня"),
        BotCommand("memes", "Мемы из Reddit (r/formuladank)"),
        BotCommand("status", "Статус бота"),
        BotCommand("clear", "Скрыть просмотренный дайджест"),
        BotCommand("clearmemes", "Очистить просмотренные мемы"),
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
    existing = load_published()
    existing_msg_ids = {p.get("channel_message_id") for p in existing}
    if msg.message_id in existing_msg_ids:
        return

    # Извлечь заголовок — первая строка текста
    title = text.split("\n")[0][:80]
    # Убрать HTML-теги из заголовка
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
    app.add_handler(CommandHandler("memes", cmd_memes))
    app.add_handler(CommandHandler("clearmemes", cmd_clearmemes))
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

    # Проверка горячих мемов
    job_queue.run_repeating(
        scheduled_meme_check,
        interval=MEME_CHECK_INTERVAL_MINUTES * 60,
        first=60,  # первая проверка через 60 секунд
    )

    return app
