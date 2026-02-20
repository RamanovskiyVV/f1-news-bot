"""
Модуль для взаимодействия с OpenAI (ChatGPT) API.
Анализ новостей, оценка хайпа, генерация постов.
"""

import json
import logging
from typing import Optional

from openai import AsyncOpenAI

from config import OPENAI_API_KEY, OPENAI_MODEL
from scraper import NewsItem

logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=OPENAI_API_KEY)


async def analyze_news_batch(news_items: list[NewsItem]) -> list[NewsItem]:
    """
    Отправить пачку новостей в ChatGPT для анализа хайпа.
    Возвращает список новостей с заполненными hype_score и summary на русском.
    """
    if not news_items:
        return []

    # Формируем список новостей для анализа
    news_list = []
    for i, item in enumerate(news_items):
        news_list.append({
            "index": i,
            "title": item.title,
            "source": item.source,
            "summary": item.summary[:300],
        })

    prompt = f"""Ты — аналитик новостей Формулы 1. Проанализируй следующие новости и для каждой:

1. Поставь оценку "хайпа" по шкале от 1 до 10, где:
   - 10: Сенсация (смена пилота топ-команды, серьёзная авария, дисквалификация, скандал)
   - 8-9: Очень важно (победа в гонке, поул, значимые контрактные новости, технические инновации)
   - 6-7: Интересно (предквалификационные расклады, тактические решения, обновления болидов)
   - 4-5: Обычные новости (пресс-конференции, рутинные обновления)
   - 1-3: Малозначительные (промо, спонсорские новости, общие заявления)

2. Напиши краткое саммари на РУССКОМ языке (1-2 предложения), чтобы было понятно о чём новость.

Верни ответ строго в JSON формате — массив объектов:
[
  {{
    "index": 0,
    "hype_score": 8,
    "summary_ru": "Краткое описание на русском"
  }},
  ...
]

Новости для анализа:
{json.dumps(news_list, ensure_ascii=False, indent=2)}
"""

    try:
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "Ты аналитик Формулы 1. Отвечай строго в JSON формате."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content
        result = json.loads(content)

        # Может вернуться как {"results": [...]} или просто [...]
        if isinstance(result, dict):
            items_data = result.get("results") or result.get("news") or result.get("items") or list(result.values())[0]
        else:
            items_data = result

        for item_data in items_data:
            idx = item_data.get("index", -1)
            if 0 <= idx < len(news_items):
                news_items[idx].hype_score = item_data.get("hype_score", 0)
                summary_ru = item_data.get("summary_ru", "")
                if summary_ru:
                    news_items[idx].summary = summary_ru

        logger.info(f"Проанализировано {len(items_data)} новостей через ChatGPT")

    except Exception as e:
        logger.error(f"Ошибка при анализе новостей через ChatGPT: {e}")

    return news_items


async def generate_news_post(
    title: str,
    url: str,
    article_content: str,
    previous_posts: list[str] | None = None,
) -> str:
    """
    Сгенерировать пост для Telegram-канала на русском языке.
    previous_posts — тексты последних постов канала для контекста стиля.
    """
    context_block = ""
    if previous_posts:
        posts_text = "\n---\n".join(previous_posts[-7:])
        context_block = f"""

Вот последние посты канала — пиши в похожем стиле и тоне, не повторяй уже опубликованную информацию:
---
{posts_text}
---
"""

    prompt = f"""Ты — автор Telegram-канала о Формуле 1. Напиши короткий, яркий и информативный пост для Telegram-канала на РУССКОМ языке на основе этой новости.

Требования:
- Пост должен быть коротким (3-6 предложений)
- Используй эмодзи для привлечения внимания (но не злоупотребляй)
- Начни с яркого заголовка, оберни его в <b>тег bold</b>
- Добавь ключевые факты
- Тон — живой, экспертный, увлекательный
- Используй HTML-теги для форматирования: <b>жирный</b>, <i>курсив</i>
- НЕ добавляй хэштеги
- НЕ добавляй ссылки
- НЕ используй Markdown (звёздочки), только HTML-теги
{context_block}
Заголовок оригинала: {title}

Текст статьи:
{article_content[:3000]}
"""

    try:
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "Ты автор популярного Telegram-канала о Формуле 1. Пиши ярко и по делу."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
        )

        post = response.choices[0].message.content.strip()
        # Конвертировать Markdown в HTML если ChatGPT всё же использовал звёздочки
        import re
        post = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', post)
        post = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', post)
        return post

    except Exception as e:
        logger.error(f"Ошибка генерации поста: {e}")
        return f"⚠️ Ошибка генерации поста. Попробуйте ещё раз."


async def find_related_post(
    new_post_title: str,
    new_post_text: str,
    published_posts: list[dict],
) -> Optional[str]:
    """
    Определить, есть ли среди опубликованных постов тематически связанный.
    Возвращает uid связанного поста или None.
    
    published_posts — список dict с ключами: uid, title, text.
    """
    if not published_posts:
        return None

    # Формируем список постов для ChatGPT (только заголовки — экономия токенов)
    posts_list = []
    for i, p in enumerate(published_posts):
        posts_list.append(f"{i}. {p.get('title', 'Без заголовка')}")

    posts_text = "\n".join(posts_list)

    prompt = f"""Ты помогаешь вести Telegram-канал о Формуле 1.

Новый пост, который будет опубликован:
Заголовок: {new_post_title}

Вот список уже опубликованных постов канала:
{posts_text}

Есть ли среди опубликованных постов тематически связанный с новым? 
Связанный — значит о ТОЙ ЖЕ теме, событии, персоне или команде (продолжение истории, обновление, развитие темы).
НЕ считай связанным посты, которые просто о Формуле 1 в целом.

Ответь строго в JSON:
{{"related_index": <номер поста или null если нет связи>, "reason": "<краткое объяснение>"}}
"""

    try:
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "Отвечай строго в JSON формате."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content
        result = json.loads(content)
        related_index = result.get("related_index")
        reason = result.get("reason", "")

        if related_index is not None and 0 <= related_index < len(published_posts):
            related = published_posts[related_index]
            logger.info(f"Найден связанный пост [{related_index}]: {reason}")
            return related.get("uid")
        else:
            logger.info(f"Связанных постов не найдено: {reason}")
            return None

    except Exception as e:
        logger.error(f"Ошибка поиска связанного поста: {e}")
        return None
