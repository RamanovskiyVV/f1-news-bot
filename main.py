"""
F1 News Bot — Точка входа.
Запускает Telegram-бота с мониторингом новостей Формулы 1.
"""

import logging
import sys

from telegram import Update

from config import TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, TELEGRAM_CHANNEL_ID
from bot import create_bot
from logging_utils import install_secret_redaction

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
install_secret_redaction()
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def validate_config():
    """Проверить что все необходимые настройки заданы."""
    errors = []
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "your_bot_token_here":
        errors.append("TELEGRAM_BOT_TOKEN не задан")
    if not OPENAI_API_KEY or OPENAI_API_KEY == "your_openai_api_key_here":
        errors.append("OPENAI_API_KEY не задан")
    if not TELEGRAM_CHANNEL_ID or TELEGRAM_CHANNEL_ID == "your_channel_here":
        errors.append("TELEGRAM_CHANNEL_ID не задан")

    if errors:
        print("❌ Ошибки конфигурации:")
        for e in errors:
            print(f"   • {e}")
        print("\nСкопируйте .env.example в .env и заполните значения.")
        sys.exit(1)


def main():
    validate_config()

    logger.info("🏎️ Запуск F1 News Bot...")
    logger.info(f"   Channel: {TELEGRAM_CHANNEL_ID}")
    logger.info("   Chat ID: будет определён при /start")

    app = create_bot()
    logger.info("✅ Бот запущен. Ожидание сообщений...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
