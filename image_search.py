"""
Модуль поиска изображений для новостных постов.
Использует Google Custom Search API (Images).
"""

import hashlib
import io
import json
import logging
from pathlib import Path

import httpx

from config import GOOGLE_API_KEY, GOOGLE_CSE_ID

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent / "image_cache.json"
MAX_CACHE = 200
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB (Telegram limit)
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _load_cache() -> dict[str, list[str]]:
    """Загрузить кэш результатов поиска."""
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _save_cache(cache: dict[str, list[str]]) -> None:
    """Сохранить кэш (FIFO, макс. MAX_CACHE записей)."""
    if len(cache) > MAX_CACHE:
        keys = list(cache.keys())
        for k in keys[: len(keys) - MAX_CACHE]:
            del cache[k]
    CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _query_key(query: str) -> str:
    """Генерировать ключ кэша для поискового запроса."""
    return hashlib.md5(query.strip().lower().encode()).hexdigest()


async def search_news_image(query: str, count: int = 5) -> list[str]:
    """
    Поиск изображений через Google Custom Search API.

    Args:
        query: поисковый запрос (заголовок новости).
        count: количество результатов (макс. 10).

    Returns:
        Список URL изображений.
    """
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        logger.warning("GOOGLE_API_KEY или GOOGLE_CSE_ID не настроены — поиск фото недоступен")
        return []

    key = _query_key(query)
    cache = _load_cache()
    if key in cache:
        logger.info(f"Изображения из кэша для: {query[:60]}")
        return cache[key][:count]

    # Добавляем "F1 Formula 1" для релевантности
    search_query = f"{query} F1 Formula 1"

    try:
        params = {
            "key": GOOGLE_API_KEY,
            "cx": GOOGLE_CSE_ID,
            "q": search_query,
            "searchType": "image",
            "num": min(count, 10),
            "imgSize": "large",
            "safe": "active",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://www.googleapis.com/customsearch/v1", params=params
            )
            resp.raise_for_status()
            data = resp.json()

        urls = []
        for item in data.get("items", []):
            link = item.get("link", "")
            if link:
                urls.append(link)

        # Кэшируем результат
        if urls:
            cache[key] = urls
            _save_cache(cache)
            logger.info(f"Найдено {len(urls)} изображений для: {query[:60]}")
        else:
            logger.info(f"Изображений не найдено для: {query[:60]}")

        return urls[:count]

    except Exception as e:
        logger.error(f"Ошибка поиска изображений: {e}")
        return []


async def download_image(url: str) -> bytes | None:
    """
    Скачать изображение по URL.

    Returns:
        bytes изображения или None при ошибке.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True, max_redirects=5
        ) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()

            # Проверить размер
            content = resp.content
            if len(content) > MAX_IMAGE_SIZE:
                logger.warning(f"Изображение слишком большое ({len(content)} байт): {url}")
                return None

            # Проверить тип контента
            content_type = resp.headers.get("content-type", "").split(";")[0].strip()
            if content_type and content_type not in ALLOWED_TYPES:
                logger.warning(f"Неподдерживаемый тип: {content_type} для {url}")
                return None

            return content

    except Exception as e:
        logger.warning(f"Ошибка скачивания изображения {url}: {e}")
        return None
