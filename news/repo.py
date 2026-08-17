import json
from datetime import datetime, timedelta

from core.db import connect


async def is_posted(url):
    async with connect() as db:
        async with db.execute("SELECT id FROM posted WHERE url=?", (url,)) as cursor:
            return await cursor.fetchone() is not None


async def mark_posted(url):
    async with connect(commit=True) as db:
        await db.execute(
            "INSERT OR IGNORE INTO posted (url, posted_at) VALUES (?, ?)",
            (url, datetime.now().isoformat()),
        )
        seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        async with db.execute(
            "SELECT COUNT(*) FROM events_log WHERE event_type='post_approved' AND created_at > ?",
            (seven_days_ago,),
        ) as cursor:
            approved = (await cursor.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM events_log WHERE event_type='post_rejected' AND created_at > ?",
            (seven_days_ago,),
        ) as cursor:
            rejected = (await cursor.fetchone())[0]
        return {"approved": approved, "rejected": rejected}


async def get_blocked_tags():
    async with connect() as db:
        async with db.execute("SELECT tag FROM blocked_tags") as cursor:
            rows = await cursor.fetchall()
            return {row[0] for row in rows}


async def add_blocked_tag(tag):
    async with connect(commit=True) as db:
        try:
            await db.execute("INSERT INTO blocked_tags (tag) VALUES (?)", (tag.lower(),))
            return True
        except Exception:
            return False


async def remove_blocked_tag(tag):
    async with connect(commit=True) as db:
        await db.execute("DELETE FROM blocked_tags WHERE tag=?", (tag.lower(),))


async def get_rss_feeds():
    async with connect() as db:
        async with db.execute("SELECT url FROM rss_feeds") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def add_rss_feed(url):
    async with connect(commit=True) as db:
        try:
            await db.execute("INSERT INTO rss_feeds (url) VALUES (?)", (url,))
            return True
        except Exception:
            return False


async def remove_rss_feed(url):
    async with connect(commit=True) as db:
        await db.execute("DELETE FROM rss_feeds WHERE url=?", (url,))


async def get_gaming_keywords():
    async with connect() as db:
        async with db.execute("SELECT keyword FROM gaming_keywords") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def add_keyword(keyword):
    async with connect(commit=True) as db:
        try:
            await db.execute(
                "INSERT INTO gaming_keywords (keyword) VALUES (?)",
                (keyword.lower(),),
            )
            return True
        except Exception:
            return False


async def remove_keyword(keyword):
    async with connect(commit=True) as db:
        await db.execute("DELETE FROM gaming_keywords WHERE keyword=?", (keyword.lower(),))


async def save_pending(title, url, summary, image_url):
    async with connect(commit=True) as db:
        try:
            await db.execute(
                "INSERT INTO pending (title, url, summary, image_url, created_at) VALUES (?, ?, ?, ?, ?)",
                (title, url, summary, image_url, datetime.now().isoformat()),
            )
            async with db.execute("SELECT last_insert_rowid()") as cursor:
                row = await cursor.fetchone()
                return row[0]
        except Exception:
            return None


async def is_pending(url):
    async with connect() as db:
        async with db.execute("SELECT id FROM pending WHERE url=?", (url,)) as cursor:
            return await cursor.fetchone() is not None


async def get_pending(pending_id):
    async with connect() as db:
        async with db.execute("SELECT * FROM pending WHERE id=?", (pending_id,)) as cursor:
            return await cursor.fetchone()


async def delete_pending(pending_id):
    async with connect(commit=True) as db:
        await db.execute("DELETE FROM pending WHERE id=?", (pending_id,))


async def get_recent_sentiments(hours=12):
    async with connect() as db:
        async with db.execute(
            """
            SELECT meta FROM events_log
            WHERE event_type='news_sentiment'
            AND created_at > datetime('now', ?)
            """,
            (f"-{hours} hours",),
        ) as cursor:
            return await cursor.fetchall()


async def get_weekly_stats():
    async with connect() as db:
        seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        async with db.execute(
            "SELECT COUNT(*) FROM events_log WHERE event_type='post_approved' AND created_at > ?",
            (seven_days_ago,),
        ) as cursor:
            approved = (await cursor.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM events_log WHERE event_type='post_rejected' AND created_at > ?",
            (seven_days_ago,),
        ) as cursor:
            rejected = (await cursor.fetchone())[0]
        return {"approved": approved, "rejected": rejected}
