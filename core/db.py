import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import aiosqlite

from config import DB_PATH

logger = logging.getLogger(__name__)

db_lock = asyncio.Lock()

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS posted (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT UNIQUE,
        posted_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pending (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        url TEXT UNIQUE,
        summary TEXT,
        image_url TEXT,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS blocked_tags (
        tag TEXT PRIMARY KEY
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        meta TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rss_feeds (
        url TEXT PRIMARY KEY,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gaming_keywords (
        keyword TEXT PRIMARY KEY,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pair TEXT,
        side TEXT,
        price REAL,
        qty REAL,
        pnl REAL,
        signal TEXT,
        sentiment TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_state (
        pair TEXT,
        key TEXT,
        value TEXT,
        PRIMARY KEY (pair, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_vacancies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        company TEXT,
        url TEXT UNIQUE,
        salary_raw TEXT,
        is_remote INTEGER,
        score INTEGER,
        match_verdict TEXT,
        has_salary INTEGER,
        dismissed INTEGER DEFAULT 0,
        applied_at TEXT,
        follow_up_sent INTEGER DEFAULT 0,
        description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_raw (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        company TEXT,
        url TEXT UNIQUE,
        salary_raw TEXT,
        is_remote INTEGER DEFAULT 1,
        source TEXT,
        description TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_score_cache (
        url TEXT PRIMARY KEY,
        score INTEGER,
        is_worldwide INTEGER,
        core_stack_match INTEGER,
        matching_skills TEXT,
        missing_skills TEXT,
        location_reason TEXT,
        verdict TEXT,
        has_salary INTEGER,
        scoring_mode TEXT,
        cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS target_companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        url TEXT UNIQUE,
        keywords TEXT,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oled_config (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """,
]

_MIGRATIONS = [
    ("job_vacancies", "applied_at", "applied_at TIMESTAMP"),
    ("job_vacancies", "follow_up_sent", "follow_up_sent INTEGER DEFAULT 0"),
    ("job_vacancies", "description", "description TEXT"),
]


def normalize_job_url(url):
    if not url:
        return None
    return url.split("#")[0].rstrip("/")


@asynccontextmanager
async def connect(*, commit=False):
    async with db_lock:
        db = await aiosqlite.connect(DB_PATH, timeout=30)
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            yield db
            if commit:
                await db.commit()
        finally:
            await db.close()


async def _ensure_column(db, table, column, ddl):
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        cols = {row[1] for row in await cursor.fetchall()}
    if column not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        logger.info("Migrated %s: added column %s", table, column)


async def init_db():
    async with connect(commit=True) as db:
        for stmt in _SCHEMA:
            await db.execute(stmt)
        for table, column, ddl in _MIGRATIONS:
            await _ensure_column(db, table, column, ddl)


async def log_event(event_type, meta=None):
    async with connect(commit=True) as db:
        meta_json = json.dumps(meta) if meta else None
        await db.execute(
            "INSERT INTO events_log (event_type, meta) VALUES (?, ?)",
            (event_type, meta_json),
        )


async def populate_initial_data(rss_feeds: list, keywords: list):
    async with connect(commit=True) as db:
        async with db.execute("SELECT COUNT(*) FROM rss_feeds") as cursor:
            if (await cursor.fetchone())[0] == 0:
                for url in rss_feeds:
                    await db.execute("INSERT OR IGNORE INTO rss_feeds (url) VALUES (?)", (url,))
        async with db.execute("SELECT COUNT(*) FROM gaming_keywords") as cursor:
            if (await cursor.fetchone())[0] == 0:
                for kw in keywords:
                    await db.execute(
                        "INSERT OR IGNORE INTO gaming_keywords (keyword) VALUES (?)",
                        (kw.lower(),),
                    )


async def cleanup_old_data():
    """Чистит старые записи чтобы не раздувать память и БД."""
    async with connect(commit=True) as db:
        await db.execute("DELETE FROM events_log WHERE created_at < datetime('now', '-7 days')")
        await db.execute("DELETE FROM pending WHERE created_at < datetime('now', '-3 days')")
        await db.execute("DELETE FROM posted WHERE posted_at < datetime('now', '-60 days')")
        await db.execute(
            "DELETE FROM job_vacancies WHERE dismissed = 1 AND created_at < datetime('now', '-30 days')"
        )
        await db.execute("DELETE FROM job_vacancies WHERE created_at < datetime('now', '-90 days')")
        await db.execute("DELETE FROM job_raw WHERE status='scored' AND created_at < datetime('now', '-14 days')")
        await db.execute("DELETE FROM job_raw WHERE created_at < datetime('now', '-30 days')")
        await db.execute("DELETE FROM job_score_cache WHERE cached_at < datetime('now', '-60 days')")
    logger.info("Database cleanup completed and old job vacancies pruned.")
