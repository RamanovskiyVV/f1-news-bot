"""
Хранилище опубликованных постов.
Сохраняет историю постов канала для:
- Контекста при генерации (стиль + избежание повторов)
- Reply на связанные посты
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PUBLISHED_FILE = Path(__file__).parent / "published_posts.json"
MAX_PUBLISHED = 50  # Хранить максимум 50 последних постов


def load_published() -> list[dict]:
    """Загрузить список опубликованных постов."""
    if PUBLISHED_FILE.exists():
        try:
            data = json.loads(PUBLISHED_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Ошибка чтения published_posts.json: {e}")
    return []


def save_published(posts: list[dict]) -> None:
    """Сохранить список опубликованных постов (макс. MAX_PUBLISHED)."""
    if len(posts) > MAX_PUBLISHED:
        posts = posts[-MAX_PUBLISHED:]
    PUBLISHED_FILE.write_text(
        json.dumps(posts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def add_published(
    uid: str,
    title: str,
    text: str,
    channel_message_id: int,
) -> None:
    """Добавить опубликованный пост в историю."""
    posts = load_published()
    posts.append({
        "uid": uid,
        "title": title,
        "text": text,
        "channel_message_id": channel_message_id,
        "timestamp": datetime.now().isoformat(),
    })
    save_published(posts)
    logger.info(f"Пост сохранён в историю: {title[:50]}... (msg_id={channel_message_id})")


def get_recent_posts(n: int = 10) -> list[dict]:
    """Получить последние N опубликованных постов."""
    posts = load_published()
    return posts[-n:]


def get_recent_posts_for_context(n: int = 7) -> list[str]:
    """Получить тексты последних N постов для контекста генерации.
    
    Возвращает только текст без ссылок (экономия токенов).
    """
    posts = get_recent_posts(n)
    texts = []
    for p in posts:
        # Убрать ссылку на источник в конце ("🔗 Источник: ...")
        text = p.get("text", "")
        lines = text.split("\n")
        # Убираем последние строки со ссылкой
        cleaned = []
        for line in lines:
            if line.strip().startswith("🔗"):
                break
            cleaned.append(line)
        texts.append("\n".join(cleaned).strip())
    return [t for t in texts if t]


def find_post_by_uid(uid: str) -> Optional[dict]:
    """Найти опубликованный пост по uid."""
    posts = load_published()
    for p in posts:
        if p.get("uid") == uid:
            return p
    return None


# --- Дневной кэш проанализированных новостей (для /digest) ---

DAILY_CACHE_FILE = Path(__file__).parent / "daily_cache.json"


def load_daily_cache() -> dict[str, list[dict]]:
    """Загрузить дневной кэш. Удаляет записи за прошлые дни."""
    today = datetime.now().strftime("%Y-%m-%d")
    if DAILY_CACHE_FILE.exists():
        try:
            data = json.loads(DAILY_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Оставить только сегодня
                if today in data:
                    return {today: data[today]}
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Ошибка чтения daily_cache.json: {e}")
    return {}


def save_daily_cache(cache: dict[str, list[dict]]) -> None:
    """Сохранить дневной кэш в файл (только сегодня)."""
    today = datetime.now().strftime("%Y-%m-%d")
    # Оставить только сегодня
    to_save = {today: cache.get(today, [])}
    DAILY_CACHE_FILE.write_text(
        json.dumps(to_save, ensure_ascii=False),
        encoding="utf-8",
    )
