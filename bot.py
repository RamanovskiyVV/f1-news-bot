"""
Telegram-бот для мониторинга новостей F1.
Обрабатывает inline-кнопки, генерацию и публикацию постов.
"""

import asyncio
import html
import json
import logging
from datetime import date
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
from scraper import NewsItem, collect_new_news, fetch_article_content
from analyzer import analyze_news_batch, generate_news_post

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
# Дневной кэш ВСЕХ проанализированных новостей (дата -> список dict)
# Хранит новости за текущий день для команды /digest
daily_news_cache: dict[str, list[dict]] = {}
# Chat ID владельца — запоминается при первом /start
owner_chat_id: Optional[int] = None


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
    photo_label = "🖼 Картинка ✅" if has_photo else "🖼 Картинка"
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
        ],
    ])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start."""
    global owner_chat_id
    owner_chat_id = update.message.chat_id
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
    action, uid = data.split(":", 1)

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

        # Генерация через ChatGPT
        post = await generate_news_post(
            title=news_data["title"],
            url=news_data["url"],
            article_content=article_content,
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

    try:
        if uid in post_photos:
            # Отправить как фото с подписью
            await context.bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo=post_photos[uid],
                caption=post[:1024],  # Telegram ограничивает caption до 1024 символов
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=post,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
        await query.message.reply_text("✅ Пост успешно отправлен в канал!")

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
        "✏️ <b>Режим редактирования</b>\n\n"
        "Отправьте мне отредактированный текст поста.\n"
        "Текущий пост скопирован ниже:\n\n"
        "─────────────────\n"
        f"{generated_posts[uid]}\n"
        "─────────────────\n\n"
        "Скопируйте, отредактируйте и отправьте мне новый вариант.\n"
        "Или отправьте /cancel для отмены.",
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
    ])
    logger.info("Меню команд установлено")


def create_bot() -> Application:
    """Создать и настроить Telegram-бота."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("status", cmd_status))

    # Inline-кнопки
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Фото (для прикрепления к постам)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))

    # Текстовые сообщения (для редактирования)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    # Автоматическая проверка по расписанию
    job_queue = app.job_queue
    job_queue.run_repeating(
        scheduled_check,
        interval=CHECK_INTERVAL_MINUTES * 60,
        first=30,  # первая проверка через 30 секунд после старта
    )

    return app
