"""
Модуль для скрапинга новостей F1 из RSS-лент и веб-страниц.
"""

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, urlunparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from config import F1_SOURCES, F1_BLUESKY_SOURCES

logger = logging.getLogger(__name__)

SEEN_FILE = "seen_news.json"


@dataclass
class NewsItem:
    """Одна новость."""
    title: str
    url: str
    source: str
    summary: str = ""
    published: str = ""
    content: str = ""
    hype_score: int = 0
    uid: str = ""

    def __post_init__(self):
        if not self.uid:
            self.uid = hashlib.md5(_normalize_url(self.url).encode()).hexdigest()


def _normalize_url(url: str) -> str:
    """Убрать query-параметры и фрагмент из URL для стабильного хэширования."""
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', '', ''))


def load_seen() -> list[str]:
    """Загрузить упорядоченный список уже обработанных UID-ов."""
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return list(data)
        except Exception:
            return []
    return []


def save_seen(seen_list: list[str]):
    """Сохранить список обработанных новостей (макс. 2000 последних, FIFO)."""
    if len(seen_list) > 2000:
        seen_list = seen_list[-2000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen_list, f)


def clear_seen() -> int:
    """Очистить список обработанных новостей. Возвращает кол-во удалённых."""
    count = len(load_seen())
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump([], f)
    return count


def _seen_set() -> set[str]:
    """Быстрый set для проверки (не для сохранения)."""
    return set(load_seen())


def fetch_rss(source: dict) -> list[NewsItem]:
    """Парсить RSS-ленту источника."""
    items = []
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = httpx.get(source["rss"], headers=headers, timeout=15, follow_redirects=True)
        feed = feedparser.parse(response.text)

        for entry in feed.entries[:15]:  # Последние 15 записей
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", "").strip()
            published = entry.get("published", "")

            # Очистить summary от HTML
            if summary:
                soup = BeautifulSoup(summary, "html.parser")
                summary = soup.get_text(separator=" ", strip=True)

            if title and link:
                items.append(NewsItem(
                    title=title,
                    url=link,
                    source=source["name"],
                    summary=summary[:500] if summary else "",
                    published=published,
                ))

    except Exception as e:
        logger.warning(f"Ошибка при парсинге RSS {source['name']}: {e}")

    return items


def fetch_bluesky(handle: str, name: str) -> list[NewsItem]:
    """Получить последние посты из Bluesky через публичный API."""
    items = []
    try:
        url = (
            f"https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
            f"?actor={handle}&limit=15&filter=posts_no_replies"
        )
        response = httpx.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()

        for entry in data.get("feed", []):
            post = entry.get("post", {})
            record = post.get("record", {})
            text = record.get("text", "").strip()
            if not text:
                continue

            # Пропустить репосты
            reason = entry.get("reason")
            if reason and reason.get("$type") == "app.bsky.feed.defs#reasonRepost":
                continue

            # Сформировать ссылку на пост
            uri = post.get("uri", "")
            parts = uri.split("/")
            post_id = parts[-1] if parts else ""
            post_url = f"https://bsky.app/profile/{handle}/post/{post_id}"

            created = record.get("createdAt", "")

            items.append(NewsItem(
                title=text[:200],
                url=post_url,
                source=name,
                summary=text[:500],
                published=created,
            ))

    except Exception as e:
        logger.warning(f"Ошибка при парсинге Bluesky {name}: {e}")

    return items


def fetch_article_content(url: str) -> str:
    """Получить текст статьи по URL для генерации новости."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
        soup = BeautifulSoup(response.text, "html.parser")

        # Удалить ненужные теги
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe"]):
            tag.decompose()

        # Попробовать найти основной контент
        article = soup.find("article")
        if article:
            text = article.get_text(separator="\n", strip=True)
        else:
            # Поиск по общим классам
            content_div = (
                soup.find("div", class_="article-content")
                or soup.find("div", class_="post-content")
                or soup.find("div", class_="entry-content")
                or soup.find("main")
            )
            if content_div:
                text = content_div.get_text(separator="\n", strip=True)
            else:
                paragraphs = soup.find_all("p")
                text = "\n".join(p.get_text(strip=True) for p in paragraphs)

        # Ограничить длину
        return text[:4000] if text else ""

    except Exception as e:
        logger.warning(f"Ошибка при получении статьи {url}: {e}")
        return ""


def collect_new_news() -> list[NewsItem]:
    """
    Собрать новые (ещё не обработанные) новости из всех источников.
    """
    seen_list = load_seen()
    seen_set = set(seen_list)
    all_news: list[NewsItem] = []

    # RSS-источники
    for source in F1_SOURCES:
        logger.info(f"Парсинг {source['name']}...")
        items = fetch_rss(source)
        new_items = [item for item in items if item.uid not in seen_set]
        all_news.extend(new_items)
        logger.info(f"  Найдено {len(items)} новостей, новых: {len(new_items)}")

    # Bluesky-инсайдеры (бесплатный публичный API)
    for bsky in F1_BLUESKY_SOURCES:
        logger.info(f"Парсинг {bsky['name']} (@{bsky['handle']})...")
        items = fetch_bluesky(bsky["handle"], bsky["name"])
        new_items = [item for item in items if item.uid not in seen_set]
        all_news.extend(new_items)
        logger.info(f"  Найдено {len(items)} постов, новых: {len(new_items)}")

    # Отметить все как просмотренные (добавляем в конец — FIFO)
    for item in all_news:
        if item.uid not in seen_set:
            seen_list.append(item.uid)
            seen_set.add(item.uid)
    save_seen(seen_list)

    logger.info(f"Всего новых новостей: {len(all_news)}")
    return all_news
