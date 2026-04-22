"""
Модуль для парсинга мемов из Reddit (r/formuladank и др.).
Использует Reddit OAuth2 API (бесплатно, 60 запросов/мин).
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from config import MEME_MIN_SCORE, MEME_MAX_AGE_HOURS, REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET

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


# ─── Reddit OAuth ─────────────────────────────────────────────────────

_reddit_token: str = ""
_reddit_token_expires: float = 0


def _get_reddit_token() -> str:
    """Получить OAuth-токен Reddit (кэшируется на ~1 час)."""
    global _reddit_token, _reddit_token_expires

    if _reddit_token and time.time() < _reddit_token_expires:
        return _reddit_token

    if not REDDIT_CLIENT_ID or not REDDIT_CLIENT_SECRET:
        raise ValueError("REDDIT_CLIENT_ID и REDDIT_CLIENT_SECRET не настроены")

    resp = httpx.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": "F1NewsMemeBot/1.0"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    _reddit_token = data["access_token"]
    _reddit_token_expires = time.time() + data.get("expires_in", 3600) - 60
    logger.info("Reddit OAuth-токен получен")
    return _reddit_token


# ─── Парсинг Reddit ──────────────────────────────────────────────────

def fetch_reddit_memes(
    subreddit: str = "formuladank",
    limit: int = 25,
) -> list[MemeItem]:
    """
    Получить мемы из сабреддита через Reddit OAuth API.

    Фильтры:
    - Только изображения (post_hint == 'image')
    - score >= MEME_MIN_SCORE
    - Не старше MEME_MAX_AGE_HOURS часов
    - Без NSFW
    """
    items: list[MemeItem] = []
    max_age_seconds = MEME_MAX_AGE_HOURS * 3600
    now = time.time()

    try:
        token = _get_reddit_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "F1NewsMemeBot/1.0",
        }
        url = f"https://oauth.reddit.com/r/{subreddit}/hot"
        params = {"limit": min(limit, 100), "raw_json": 1}

        response = httpx.get(url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})

            # Фильтры
            if post.get("over_18", False):
                continue
            if post.get("post_hint") != "image":
                continue
            score = post.get("score", 0)
            if score < MEME_MIN_SCORE:
                continue
            created = post.get("created_utc", 0)
            if now - created > max_age_seconds:
                continue

            image_url = post.get("url_overridden_by_dest", "") or post.get("url", "")
            if not image_url or not any(
                image_url.lower().endswith(ext)
                for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")
            ):
                # Попробовать preview
                preview = post.get("preview", {})
                images = preview.get("images", [])
                if images:
                    image_url = images[0].get("source", {}).get("url", "")
                if not image_url:
                    continue

            permalink = post.get("permalink", "")
            full_permalink = f"https://reddit.com{permalink}" if permalink else ""

            items.append(MemeItem(
                title=post.get("title", ""),
                image_url=image_url,
                score=score,
                permalink=full_permalink,
                subreddit=subreddit,
                created_utc=created,
            ))

        logger.info(f"Reddit r/{subreddit}: найдено {len(items)} мемов (score>={MEME_MIN_SCORE}, <{MEME_MAX_AGE_HOURS}h)")

    except Exception as e:
        logger.error(f"Ошибка парсинга Reddit r/{subreddit}: {e}")

    return items


def collect_new_memes() -> list[MemeItem]:
    """
    Собрать новые (непросмотренные) мемы из всех источников.
    Сортировка: по score убывание.
    """
    seen_data = load_seen_memes()
    seen_set = set(seen_data["seen"]) | set(seen_data["published"])

    all_memes: list[MemeItem] = []

    # r/formuladank — основной источник F1 мемов
    memes = fetch_reddit_memes("formuladank", limit=25)
    new_memes = [m for m in memes if m.uid not in seen_set]
    all_memes.extend(new_memes)

    # Сортировка по score
    all_memes.sort(key=lambda m: m.score, reverse=True)

    logger.info(f"Всего новых мемов: {len(all_memes)}")
    return all_memes
