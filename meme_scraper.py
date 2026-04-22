"""
Модуль для парсинга мемов из Reddit (r/formuladank и др.).
Использует Reddit RSS-фид + парсинг HTML для извлечения картинок.
Не требует авторизации и API-ключей.
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import feedparser
import httpx
from bs4 import BeautifulSoup

from config import MEME_MAX_AGE_HOURS

logger = logging.getLogger(__name__)

SEEN_MEMES_FILE = Path(__file__).parent / "seen_memes.json"
MAX_SEEN = 500


@dataclass
class MemeItem:
    """Один мем из Reddit."""
    title: str
    image_url: str
    score: int
    permalink: str
    subreddit: str
    uid: str = ""
    created_utc: float = 0.0

    def __post_init__(self):
        if not self.uid:
            self.uid = hashlib.md5(self.permalink.encode()).hexdigest()


# ─── Seen-менеджмент ──────────────────────────────────────────────────

def load_seen_memes() -> dict:
    """Загрузить списки просмотренных и опубликованных мемов.

    Returns:
        {"seen": [...], "published": [...]}
    """
    if SEEN_MEMES_FILE.exists():
        try:
            data = json.loads(SEEN_MEMES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "seen": list(data.get("seen", [])),
                    "published": list(data.get("published", [])),
                }
        except (json.JSONDecodeError, ValueError):
            pass
    return {"seen": [], "published": []}


def save_seen_memes(data: dict) -> None:
    """Сохранить seen-списки мемов (FIFO)."""
    seen = data.get("seen", [])
    if len(seen) > MAX_SEEN:
        seen = seen[-MAX_SEEN:]
    data["seen"] = seen
    SEEN_MEMES_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def mark_meme_seen(uid: str) -> None:
    """Пометить мем как просмотренный."""
    data = load_seen_memes()
    if uid not in data["seen"]:
        data["seen"].append(uid)
    save_seen_memes(data)


def mark_meme_published(uid: str) -> None:
    """Пометить мем как опубликованный."""
    data = load_seen_memes()
    if uid not in data["published"]:
        data["published"].append(uid)
    if uid not in data["seen"]:
        data["seen"].append(uid)
    save_seen_memes(data)


def clear_seen_memes() -> int:
    """Очистить список просмотренных мемов (not published). Вернуть количество."""
    data = load_seen_memes()
    count = len(data["seen"])
    data["seen"] = []
    save_seen_memes(data)
    return count


# ─── Парсинг Reddit RSS ───────────────────────────────────────────────

def fetch_reddit_memes(
    subreddit: str = "formuladank",
    limit: int = 25,
) -> list[MemeItem]:
    """
    Получить мемы из сабреддита через RSS-фид.
    RSS не требует авторизации и не блокируется на EC2.

    Фильтры:
    - Только посты с картинками (ищем <img> в контенте)
    - Не старше MEME_MAX_AGE_HOURS часов
    """
    items: list[MemeItem] = []
    max_age_seconds = MEME_MAX_AGE_HOURS * 3600
    now = time.time()

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        rss_url = f"https://www.reddit.com/r/{subreddit}/hot.rss?limit={min(limit, 100)}"

        response = httpx.get(rss_url, headers=headers, timeout=15, follow_redirects=True)
        response.raise_for_status()
        feed = feedparser.parse(response.text)

        for entry in feed.entries:
            # Проверить возраст
            published = entry.get("updated_parsed") or entry.get("published_parsed")
            if published:
                entry_time = time.mktime(published)
                if now - entry_time > max_age_seconds:
                    continue

            # Извлечь картинку из HTML-контента
            content_html = ""
            if hasattr(entry, "content") and entry.content:
                content_html = entry.content[0].get("value", "")
            elif hasattr(entry, "summary"):
                content_html = entry.summary or ""

            if not content_html:
                continue

            # Ищем картинку в контенте
            image_url = _extract_image_url(content_html)
            if not image_url:
                continue

            permalink = entry.get("link", "")
            title = entry.get("title", "").strip()
            if not title or not permalink:
                continue

            items.append(MemeItem(
                title=title,
                image_url=image_url,
                score=0,  # RSS не даёт score
                permalink=permalink,
                subreddit=subreddit,
                created_utc=time.mktime(published) if published else now,
            ))

        logger.info(f"Reddit RSS r/{subreddit}: найдено {len(items)} мемов (<{MEME_MAX_AGE_HOURS}h)")

    except Exception as e:
        logger.error(f"Ошибка парсинга Reddit RSS r/{subreddit}: {e}")

    return items


def _extract_image_url(html_content: str) -> str:
    """Извлечь URL картинки из HTML-контента RSS-записи Reddit."""
    soup = BeautifulSoup(html_content, "html.parser")

    # 1. Ищем <img> теги (основной способ)
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if _is_meme_image(src):
            return src

    # 2. Ищем прямые ссылки на картинки в <a> тегах
    for a in soup.find_all("a"):
        href = a.get("href", "")
        if _is_meme_image(href):
            return href

    # 3. Ищем i.redd.it ссылки в тексте
    text = soup.get_text()
    match = re.search(r'https?://i\.redd\.it/\S+\.(?:jpg|jpeg|png|gif|webp)', text)
    if match:
        return match.group(0)

    return ""


def _is_meme_image(url: str) -> bool:
    """Проверить, является ли URL картинкой-мемом (не иконкой/аватаркой)."""
    if not url:
        return False
    url_lower = url.lower()
    # Пропускаем маленькие иконки Reddit
    if "emoji" in url_lower or "icon" in url_lower or "avatar" in url_lower:
        return False
    if "styles.redditmedia.com" in url_lower:
        return False
    # Принимаем i.redd.it и preview.redd.it (основные хосты картинок Reddit)
    if "i.redd.it" in url_lower or "preview.redd.it" in url_lower:
        return True
    # Принимаем imgur
    if "imgur.com" in url_lower:
        return True
    # Принимаем прямые ссылки на картинки
    if any(url_lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
        return True
    return False


def collect_new_memes() -> list[MemeItem]:
    """
    Собрать новые (непросмотренные) мемы из всех источников.
    """
    seen_data = load_seen_memes()
    seen_set = set(seen_data["seen"]) | set(seen_data["published"])

    all_memes: list[MemeItem] = []

    # r/formuladank — основной источник F1 мемов
    memes = fetch_reddit_memes("formuladank", limit=25)
    new_memes = [m for m in memes if m.uid not in seen_set]
    all_memes.extend(new_memes)

    logger.info(f"Всего новых мемов: {len(all_memes)}")
    return all_memes
