"""One resumable pipeline shared by /check and scheduled checks."""
import asyncio
import logging

from analyzer import analyze_news_batch, deduplicate_news
from config import HYPE_THRESHOLD, NEWS_MAX_PER_CHECK, NEWS_REVIEW_MIN_SCORE, OPENAI_MODEL_REVIEW
from news_queue import NewsQueue
from scraper import collect_new_news, fetch_article_content, load_seen, save_seen

logger = logging.getLogger(__name__)
_check_lock = asyncio.Lock()


async def run_news_check(send, cache, queue=None):
    async with _check_lock:
        own_queue = queue is None
        queue = queue or NewsQueue()
        stats = {"analyzed": 0, "sent": 0, "duplicates": 0, "errors": 0}
        try:
            # Network fetching runs off the event loop. SQLite stays on its owner thread.
            source_counts = []
            fresh = await asyncio.to_thread(collect_new_news, source_counts)
            queue.enqueue(fresh)
            for name, count in source_counts:
                queue.record_source(name, count)
            # Legacy seen file is only an ingestion optimisation, never the queue.
            seen = load_seen()
            save_seen(list(dict.fromkeys(seen + [n.uid for n in fresh])))

            work = queue.ready(NEWS_MAX_PER_CHECK)
            pending = [n for n, stage in work if stage == 'pending']
            for start in range(0, len(pending), 10):
                batch = pending[start:start + 10]
                try:
                    await analyze_news_batch(batch)
                    for item in batch:
                        review = item.needs_context or NEWS_REVIEW_MIN_SCORE <= max(
                            item.hype_score, item.importance_score
                        ) < HYPE_THRESHOLD
                        queue.save(item, 'review' if review else 'ready')
                        stats['analyzed'] += 1
                except Exception as exc:
                    logger.warning("News analysis failed: %s", type(exc).__name__)
                    for item in batch:
                        queue.fail(item, exc)
                    stats['errors'] += len(batch)

            # Snapshot bounds the work per cycle, including retries from previous cycles.
            eligible = {n.uid for n, _ in work}
            for item, stage in queue.ready(NEWS_MAX_PER_CHECK):
                if item.uid not in eligible or stage == 'pending':
                    continue
                try:
                    if stage == 'review':
                        if not item.content:
                            item.content = await asyncio.to_thread(fetch_article_content, item.url)
                        # Missing article text is explicit context, not a reason to silently discard.
                        await analyze_news_batch([item], model=OPENAI_MODEL_REVIEW)
                        queue.save(item, 'ready')
                    cache([item])
                    if max(item.hype_score, item.importance_score) < HYPE_THRESHOLD:
                        queue.save(item, 'digest' if max(item.hype_score, item.importance_score) >= 3 else 'skipped')
                        continue
                    # Compare against confirmed deliveries, including earlier items in this cycle.
                    kept = await deduplicate_news([item], queue.sent())
                    if not kept:
                        queue.save(item, 'duplicate')
                        stats['duplicates'] += 1
                        continue
                    await send(item)
                    queue.save(item, 'sent')
                    stats['sent'] += 1
                except Exception as exc:
                    logger.warning("News %s retained for retry: %s", item.uid, type(exc).__name__)
                    queue.fail(item, exc)
                    stats['errors'] += 1
            return stats
        finally:
            if own_queue:
                queue.close()
