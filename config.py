import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
OPENAI_MODEL_GENERATE = os.getenv("OPENAI_MODEL_GENERATE", "gpt-5.2")

# Scraping
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))
HYPE_THRESHOLD = int(os.getenv("HYPE_THRESHOLD", "8"))

# Google Custom Search (для поиска фото к новостям)
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID", "")

# Мемы (Reddit)
MEME_MIN_SCORE = int(os.getenv("MEME_MIN_SCORE", "100"))
MEME_HOT_SCORE = int(os.getenv("MEME_HOT_SCORE", "500"))
MEME_CHECK_INTERVAL_MINUTES = int(os.getenv("MEME_CHECK_INTERVAL_MINUTES", "30"))
MEME_MAX_AGE_HOURS = int(os.getenv("MEME_MAX_AGE_HOURS", "24"))

# Список источников F1
F1_SOURCES = [
    {
        "name": "Formula1.com",
        "url": "https://www.formula1.com/en/latest/all",
        "rss": "https://www.formula1.com/content/fom-website/en/latest/all.xml",
        "type": "rss",
    },
    {
        "name": "Autosport",
        "url": "https://www.autosport.com/f1/news/",
        "rss": "https://www.autosport.com/rss/feed/f1",
        "type": "rss",
    },
    {
        "name": "Motorsport.com",
        "url": "https://www.motorsport.com/f1/news/",
        "rss": "https://www.motorsport.com/rss/f1/news/",
        "type": "rss",
    },
    {
        "name": "RaceFans",
        "url": "https://www.racefans.net/",
        "rss": "https://www.racefans.net/feed/",
        "type": "rss",
    },
    {
        "name": "PlanetF1",
        "url": "https://www.planetf1.com/news/",
        "rss": "https://www.planetf1.com/feed/",
        "type": "rss",
    },
    {
        "name": "The Race",
        "url": "https://the-race.com/formula-1/",
        "rss": "https://the-race.com/feed/",
        "type": "rss",
    },
    {
        "name": "Crash.net",
        "url": "https://www.crash.net/f1/news",
        "rss": "https://www.crash.net/rss/f1/news",
        "type": "rss",
    },
    {
        "name": "GPFans",
        "url": "https://www.gpfans.com/en/f1-news/",
        "rss": "https://www.gpfans.com/en/rss.xml",
        "type": "rss",
    },
]

# Bluesky-инсайдеры F1 (бесплатный публичный API)
F1_BLUESKY_SOURCES = [
    {
        "name": "🦋 Chris Medland",
        "handle": "chrismedlandf1.bsky.social",
    },
    {
        "name": "🦋 Albert Fabrega",
        "handle": "albertfabrega.bsky.social",
    },
    {
        "name": "🦋 Andrew Benson",
        "handle": "andrewbensonf1.bsky.social",
    },
    {
        "name": "🦋 Adam Cooper",
        "handle": "adamcooperf1.bsky.social",
    },
]
