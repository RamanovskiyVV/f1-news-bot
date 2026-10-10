"""Durable news inbox and decision audit. SQLite is part of Python's stdlib."""
import json
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from config import NEWS_MAX_AGE_HOURS
from scraper import NewsItem

QUEUE_FILE = Path(__file__).parent / "news_queue.sqlite3"


def _published_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    for parse in (lambda v: datetime.fromisoformat(v.replace('Z', '+00:00')), parsedate_to_datetime):
        try:
            dt = parse(value)
            if dt.tzinfo is None:
                return None  # Ambiguous timestamps should not cause a silent rejection.
            return dt.astimezone(timezone.utc).timestamp()
        except (ValueError, TypeError, OverflowError):
            pass
    return None


class NewsQueue:
    def __init__(self, path=QUEUE_FILE):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS news (
                uid TEXT PRIMARY KEY, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                created REAL NOT NULL, updated REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS news_state ON news(state, retry_at);
            CREATE TABLE IF NOT EXISTS sources (
                name TEXT PRIMARY KEY, checked REAL, last_ok REAL, count INTEGER
            );
        """)

    def close(self):
        self.db.close()

    def enqueue(self, items):
        now = time.time()
        records = []
        for item in items:
            published = _published_timestamp(item.published)
            state = 'pending'
            if published is not None and now - published > NEWS_MAX_AGE_HOURS * 3600:
                state = 'archived'
                item.decision_reason = 'Older than the configured ingestion window'
            records.append((item.uid, json.dumps(asdict(item), ensure_ascii=False), state, now, now))
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO news(uid,payload,state,created,updated) VALUES(?,?,?,?,?)",
                records,
            )

    def ready(self, limit):
        rows = self.db.execute(
            "SELECT payload,state FROM news WHERE state IN ('pending','review','ready') "
            "AND retry_at<=? ORDER BY CASE state WHEN 'ready' THEN 0 ELSE 1 END, created LIMIT ?",
            (time.time(), limit),
        )
        return [(NewsItem(**json.loads(r['payload'])), r['state']) for r in rows]

    def save(self, item, state):
        with self.db:
            self.db.execute(
                "UPDATE news SET payload=?,state=?,updated=?,retry_at=0,error='',attempts=0 WHERE uid=?",
                (json.dumps(asdict(item), ensure_ascii=False), state, time.time(), item.uid),
            )

    def fail(self, item, error):
        # Keep the stage intact; retry after 1, 2, 4 ... minutes, capped at an hour.
        with self.db:
            self.db.execute(
                "UPDATE news SET attempts=attempts+1,error=?,updated=?,"
                "retry_at=? + MIN(3600,60 * (1 << MIN(attempts,6))) WHERE uid=?",
                (type(error).__name__, time.time(), time.time(), item.uid),
            )

    def sent(self):
        rows = self.db.execute(
            "SELECT payload FROM news WHERE state='sent' AND updated>=? ORDER BY updated DESC LIMIT 200",
            (time.time() - 48 * 3600,),
        )
        return [json.loads(r['payload']) for r in rows]

    def record_source(self, name, count):
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO sources VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "checked=excluded.checked,count=excluded.count,"
                "last_ok=CASE WHEN excluded.count>0 THEN excluded.checked ELSE sources.last_ok END",
                (name, now, now if count else None, count),
            )

    def status(self):
        counts = dict(self.db.execute("SELECT state,COUNT(*) FROM news GROUP BY state"))
        retrying = self.db.execute("SELECT COUNT(*) FROM news WHERE error!=''").fetchone()[0]
        sources = [dict(r) for r in self.db.execute("SELECT * FROM sources ORDER BY name")]
        return counts, retrying, sources
