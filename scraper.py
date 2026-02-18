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

import feedparser
import httpx
from bs4 import BeautifulSoup

from config import F1_SOURCES

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
            self.uid = hashlib.md5(self.url.encode()).hexdigest()


def load_seen() -> set[str]:
    """Загрузить список уже обработанных новостей."""
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data)
        except Exception:
            return set()
    return set()


def save_seen(seen: set[str]):
    """Сохранить список обработанных новостей."""
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f)


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
    seen = load_seen()
    all_news: list[NewsItem] = []

    for source in F1_SOURCES:
        logger.info(f"Парсинг {source['name']}...")
        items = fetch_rss(source)
        new_items = [item for item in items if item.uid not in seen]
        all_news.extend(new_items)
        logger.info(f"  Найдено {len(items)} новостей, новых: {len(new_items)}")

    # Отметить все как просмотренные
    for item in all_news:
        seen.add(item.uid)
    save_seen(seen)

    logger.info(f"Всего новых новостей: {len(all_news)}")
    return all_news
