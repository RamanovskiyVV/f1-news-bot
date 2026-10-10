"""Offline regression tests: no real API calls, Telegram sends or production files."""
import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault('OPENAI_API_KEY', 'offline-test-key')

import analyzer
import news_pipeline as pipeline
import scraper
from news_queue import NewsQueue
from scraper import NewsItem


def news(number=1):
    return NewsItem(f'FIA decision {number}', f'https://example.org/{number}', 'FIA', 'Original facts')


def response(rows):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason='stop', message=SimpleNamespace(content=json.dumps({'results': rows}))
    )])


def score(index=0, hype=8, importance=8, context=False):
    return dict(index=index, hype_score=hype, importance_score=importance,
                confidence='high', needs_context=context, summary_ru='Facts', reason='Sporting consequences')


class AnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_response_retries_instead_of_zero_scoring(self):
        items = [news(1), news(2)]
        with patch.object(analyzer.client.chat.completions, 'create', AsyncMock(return_value=response([score()]))):
            with self.assertRaises(ValueError):
                await analyzer.analyze_news_batch(items)
        self.assertTrue(all(n.hype_score == 0 for n in items))

    async def test_invalid_scores_do_not_partially_modify_batch(self):
        items = [news(1), news(2)]
        for bad in (score(1, hype='8'), score(0), score(1, hype=True), score(1, importance=11)):
            with self.subTest(bad=bad):
                with patch.object(analyzer.client.chat.completions, 'create', AsyncMock(return_value=response([score(), bad]))):
                    with self.assertRaises(ValueError):
                        await analyzer.analyze_news_batch(items)
                self.assertEqual([n.hype_score for n in items], [0, 0])

    async def test_review_preserves_original_source_description(self):
        item = news()
        create = AsyncMock(return_value=response([score()]))
        with patch.object(analyzer.client.chat.completions, 'create', create):
            await analyzer.analyze_news_batch([item])
            item.content = 'Full article facts'
            await analyzer.analyze_news_batch([item], model='review-model')
        sent = json.loads(create.call_args.kwargs['messages'][1]['content'])[0]
        self.assertEqual(sent['summary'], 'Original facts')
        self.assertEqual(sent['article'], 'Full article facts')

    async def test_update_and_uncertain_are_kept_duplicate_requires_delivered_uid(self):
        sent = [asdict(news(10))]
        for decision in ('update', 'uncertain', 'new', 'duplicate'):
            row = dict(index=0, decision=decision, duplicate_of=sent[0]['uid'], reason='Specific fact')
            with patch.object(analyzer.client.chat.completions, 'create', AsyncMock(return_value=response([row]))):
                kept = await analyzer.deduplicate_news([news()], sent)
            self.assertEqual(len(kept), 0 if decision == 'duplicate' else 1)
        row['duplicate_of'] = 'unknown'
        with patch.object(analyzer.client.chat.completions, 'create', AsyncMock(return_value=response([row]))):
            with self.assertRaises(ValueError):
                await analyzer.deduplicate_news([news()], sent)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'queue.sqlite3'
        self.queue = NewsQueue(self.path)
        self.fresh = [news()]
        self.send = AsyncMock()
        self.cache = Mock()
        self.patches = [
            patch.object(pipeline, 'collect_new_news', side_effect=lambda counts: self.fresh),
            patch.object(pipeline, 'load_seen', return_value=[]),
            patch.object(pipeline, 'save_seen'),
            patch.object(pipeline, 'fetch_article_content', return_value='Full article'),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.queue.close()
        self.temp.cleanup()

    async def run_check(self):
        return await pipeline.run_news_check(self.send, self.cache, self.queue)

    def release_retries(self):
        with self.queue.db:
            self.queue.db.execute('UPDATE news SET retry_at=0')

    async def test_api_failure_survives_restart_and_leaving_feed(self):
        with patch.object(pipeline, 'analyze_news_batch', AsyncMock(side_effect=RuntimeError('offline'))):
            stats = await self.run_check()
        self.assertEqual(stats['errors'], 1)
        self.send.assert_not_awaited()
        self.assertEqual(self.queue.status()[0], {'pending': 1})
        self.queue.close()
        self.queue = NewsQueue(self.path)
        self.fresh = []
        self.release_retries()
        with patch.object(analyzer.client.chat.completions, 'create', AsyncMock(return_value=response([score()]))):
            await self.run_check()
        self.send.assert_awaited_once()
        self.assertEqual(self.queue.status()[0], {'sent': 1})

    async def test_telegram_failure_does_not_lose_remaining_items_or_reanalyze(self):
        self.fresh = [news(1), news(2)]
        self.send.side_effect = [RuntimeError('offline'), None]
        with patch.object(analyzer.client.chat.completions, 'create', AsyncMock(return_value=response([score(0), score(1)]))):
            await self.run_check()
        self.assertEqual(self.queue.status()[0], {'ready': 1, 'sent': 1})
        self.release_retries()
        self.fresh = []
        self.send.side_effect = None
        with patch.object(pipeline, 'analyze_news_batch', AsyncMock()) as analyze, patch.object(
            pipeline, 'deduplicate_news', AsyncMock(side_effect=lambda items, sent: items)
        ):
            await self.run_check()
        analyze.assert_not_awaited()
        self.assertEqual(self.queue.status()[0], {'sent': 2})

    async def test_quiet_but_important_story_alerts(self):
        with patch.object(analyzer.client.chat.completions, 'create', AsyncMock(return_value=response([score(hype=4, importance=9)]))):
            await self.run_check()
        self.send.assert_awaited_once()

    async def test_borderline_gets_article_review_before_alert(self):
        create = AsyncMock(side_effect=[response([score(hype=6, importance=7)]), response([score(importance=9)])])
        with patch.object(analyzer.client.chat.completions, 'create', create):
            await self.run_check()
        self.assertEqual(create.await_count, 2)
        self.assertEqual(create.call_args.kwargs['model'], pipeline.OPENAI_MODEL_REVIEW)
        self.assertIn('Full article', create.call_args.kwargs['messages'][1]['content'])
        self.send.assert_awaited_once()

    async def test_review_failure_resumes_review_not_initial_analysis(self):
        create = AsyncMock(side_effect=[response([score(hype=6, importance=7)]), RuntimeError('offline')])
        with patch.object(analyzer.client.chat.completions, 'create', create):
            await self.run_check()
        self.assertEqual(self.queue.status()[0], {'review': 1})
        self.release_retries()
        create = AsyncMock(return_value=response([score()]))
        with patch.object(analyzer.client.chat.completions, 'create', create):
            await self.run_check()
        self.assertEqual(create.await_count, 1)
        self.assertEqual(create.call_args.kwargs['model'], pipeline.OPENAI_MODEL_REVIEW)

    async def test_confirmed_deliveries_deduplicate_same_cycle_and_after_restart(self):
        self.fresh = [news(1), news(2)]
        async def dedup(items, sent):
            if sent:
                items[0].duplicate_of = sent[0]['uid']
                items[0].dedup_reason = 'Same decision'
                return []
            return items
        with patch.object(analyzer.client.chat.completions, 'create', AsyncMock(return_value=response([score(0), score(1)]))), patch.object(pipeline, 'deduplicate_news', side_effect=dedup):
            await self.run_check()
        self.send.assert_awaited_once()
        self.queue.close()
        self.queue = NewsQueue(self.path)
        self.assertEqual(len(self.queue.sent()), 1)
        self.assertEqual(self.queue.status()[0], {'duplicate': 1, 'sent': 1})

    async def test_manual_and_scheduled_overlap_only_delivers_once(self):
        with patch.object(analyzer.client.chat.completions, 'create', AsyncMock(return_value=response([score()]))):
            await asyncio.gather(self.run_check(), self.run_check())
        self.send.assert_awaited_once()

    async def test_dedup_failure_stays_ready_and_retries(self):
        previous = news(10)
        self.queue.enqueue([previous])
        self.queue.save(previous, 'sent')
        with patch.object(analyzer.client.chat.completions, 'create', AsyncMock(return_value=response([score()]))), patch.object(
            pipeline, 'deduplicate_news', AsyncMock(side_effect=ValueError('invalid response'))
        ):
            await self.run_check()
        self.send.assert_not_awaited()
        self.assertEqual(self.queue.status()[0], {'ready': 1, 'sent': 1})

    async def test_old_newly_discovered_items_are_archived_but_existing_work_is_retained(self):
        self.fresh[0].published = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        with patch.object(pipeline, 'analyze_news_batch', AsyncMock()) as analyze:
            await self.run_check()
        analyze.assert_not_awaited()
        self.assertEqual(self.queue.status()[0], {'archived': 1})
        pending = news(2)
        self.queue.enqueue([pending])
        pending.published = self.fresh[0].published
        self.queue.enqueue([pending])
        self.assertEqual(self.queue.status()[0], {'archived': 1, 'pending': 1})

    async def test_backoff_prevents_immediate_retry(self):
        with patch.object(pipeline, 'analyze_news_batch', AsyncMock(side_effect=RuntimeError())) as analyze:
            await self.run_check()
            await self.run_check()
        self.assertEqual(analyze.await_count, 1)

    def test_source_health_survives_restart_and_retains_last_success(self):
        self.queue.record_source('RSS', 10)
        self.queue.record_source('RSS', 0)
        self.queue.close()
        self.queue = NewsQueue(self.path)
        source = self.queue.status()[2][0]
        self.assertEqual(source['count'], 0)
        self.assertIsNotNone(source['last_ok'])


class BotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_and_scheduled_commands_use_shared_pipeline(self):
        import bot
        progress = SimpleNamespace(edit_text=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=123),
            message=SimpleNamespace(reply_text=AsyncMock(return_value=progress),
                                    chat=SimpleNamespace(send_message=AsyncMock())),
        )
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        async def run(send, cache):
            await send(news())
            return dict(analyzed=1, sent=1, duplicates=0, errors=0)
        with patch.object(bot, 'owner_chat_id', 123), patch.object(bot, 'news_cache', {}), patch.object(
            bot, 'run_news_check', AsyncMock(side_effect=run)
        ) as shared:
            await bot.cmd_check(update, context)
            await bot.scheduled_check(context)
        self.assertEqual(shared.await_count, 2)
        update.message.chat.send_message.assert_awaited_once()
        context.bot.send_message.assert_awaited_once()
        self.assertEqual(context.bot.send_message.call_args.kwargs['chat_id'], 123)


class ScraperTests(unittest.TestCase):
    def test_collection_does_not_mark_seen_and_collapses_same_url(self):
        with patch.object(scraper, 'F1_SOURCES', [{'name': 'A'}, {'name': 'B'}]), patch.object(
            scraper, 'F1_BLUESKY_SOURCES', []
        ), patch.object(scraper, 'load_seen', return_value=[]), patch.object(
            scraper, 'fetch_rss', return_value=[news()]
        ), patch.object(scraper, 'save_seen') as save:
            self.assertEqual(len(scraper.collect_new_news()), 1)
            save.assert_not_called()

    def test_rss_http_failure_is_reported_and_not_parsed(self):
        r = Mock()
        r.raise_for_status.side_effect = RuntimeError('HTTP 503')
        with patch.object(scraper.httpx, 'get', return_value=r), patch.object(scraper.feedparser, 'parse') as parse:
            with self.assertLogs('scraper', level='WARNING'):
                self.assertEqual(scraper.fetch_rss({'rss': 'https://example.org', 'name': 'A'}), [])
            parse.assert_not_called()

    def test_rss_is_not_limited_to_fifteen(self):
        entries = [{'title': str(i), 'link': f'https://example.org/{i}'} for i in range(30)]
        with patch.object(scraper.httpx, 'get', return_value=Mock()), patch.object(
            scraper.feedparser, 'parse', return_value=SimpleNamespace(entries=entries)
        ):
            self.assertEqual(len(scraper.fetch_rss({'rss': 'https://example.org', 'name': 'A'})), 30)


if __name__ == '__main__':
    unittest.main()
