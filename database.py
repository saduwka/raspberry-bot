import aiosqlite
import json
import logging
import asyncio
from datetime import datetime
from config import DB_PATH

logger = logging.getLogger(__name__)

# Глобальная блокировка для предотвращения database is locked в SQLite
db_lock = asyncio.Lock()

async def init_db():
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            
            # Существующие таблицы
            await db.execute("""
                CREATE TABLE IF NOT EXISTS posted (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE,
                    posted_at TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS pending (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT,
                    url TEXT UNIQUE,
                    summary TEXT,
                    image_url TEXT,
                    created_at TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS watches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    name TEXT,
                    url TEXT UNIQUE,
                    target_price INTEGER,
                    last_price INTEGER,
                    service TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watch_id INTEGER,
                    price INTEGER,
                    found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (watch_id) REFERENCES watches(id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS price_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watch_id INTEGER,
                    price INTEGER,
                    item_name TEXT,
                    item_url TEXT,
                    found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    sent_at TIMESTAMP,
                    FOREIGN KEY (watch_id) REFERENCES watches(id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS blocked_tags (
                    tag TEXT PRIMARY KEY
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS events_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    meta TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # НОВЫЕ ТАБЛИЦЫ ДЛЯ ГИБКОЙ НАСТРОЙКИ
            await db.execute("""
                CREATE TABLE IF NOT EXISTS rss_feeds (
                    url TEXT PRIMARY KEY,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS gaming_keywords (
                    keyword TEXT PRIMARY KEY,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await db.commit()

async def log_event(event_type, meta=None):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            meta_json = json.dumps(meta) if meta else None
            await db.execute("INSERT INTO events_log (event_type, meta) VALUES (?, ?)", (event_type, meta_json))
            await db.commit()

# --- Watches Management ---
async def add_watch(user_id, name, url, target_price, last_price, service):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            try:
                await db.execute("INSERT INTO watches (user_id, name, url, target_price, last_price, service) VALUES (?, ?, ?, ?, ?, ?)",
                                 (user_id, name, url, target_price, last_price, service))
                await db.commit()
                
                async with db.execute("SELECT last_insert_rowid()") as cursor:
                    row = await cursor.fetchone()
                    watch_id = row[0]
                    
                if last_price is not None:
                    await db.execute("INSERT INTO price_history (watch_id, price) VALUES (?, ?)", (watch_id, last_price))
                    await db.commit()
                return True
            except Exception as e:
                logger.error(f"Error adding watch: {e}")
                return False

async def get_watches():
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("SELECT * FROM watches") as cursor:
                return await cursor.fetchall()

async def update_watch_price(watch_id, price):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            async with db.execute("SELECT last_price FROM watches WHERE id=?", (watch_id,)) as cursor:
                row = await cursor.fetchone()
                if not row: return
                last_price = row[0]
            
            if last_price != price:
                await db.execute("UPDATE watches SET last_price=? WHERE id=?", (price, watch_id))
                await db.execute("INSERT INTO price_history (watch_id, price) VALUES (?, ?)", (watch_id, price))
                await db.commit()

async def remove_watch(watch_id):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("DELETE FROM watches WHERE id=?", (watch_id,))
            await db.commit()

# --- News Status ---
async def is_posted(url):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("SELECT id FROM posted WHERE url=?", (url,)) as cursor:
                result = await cursor.fetchone()
                return result is not None

async def mark_posted(url):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("INSERT OR IGNORE INTO posted (url, posted_at) VALUES (?, ?)",
                             (url, datetime.now().isoformat()))
            await db.commit()

# --- Tags ---
async def get_blocked_tags():
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("SELECT tag FROM blocked_tags") as cursor:
                rows = await cursor.fetchall()
                return {row[0] for row in rows}

async def add_blocked_tag(tag):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            try:
                await db.execute("INSERT INTO blocked_tags (tag) VALUES (?)", (tag.lower(),))
                await db.commit()
                return True
            except:
                return False

async def remove_blocked_tag(tag):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("DELETE FROM blocked_tags WHERE tag=?", (tag.lower(),))
            await db.commit()

# --- RSS Feeds Management ---
async def get_rss_feeds():
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("SELECT url FROM rss_feeds") as cursor:
                rows = await cursor.fetchall()
                return [row[0] for row in rows]

async def add_rss_feed(url):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            try:
                await db.execute("INSERT INTO rss_feeds (url) VALUES (?)", (url,))
                await db.commit()
                return True
            except:
                return False

async def remove_rss_feed(url):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("DELETE FROM rss_feeds WHERE url=?", (url,))
            await db.commit()

# --- Keywords Management ---
async def get_gaming_keywords():
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("SELECT keyword FROM gaming_keywords") as cursor:
                rows = await cursor.fetchall()
                return [row[0] for row in rows]

async def add_keyword(keyword):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            try:
                await db.execute("INSERT INTO gaming_keywords (keyword) VALUES (?)", (keyword.lower(),))
                await db.commit()
                return True
            except:
                return False

async def remove_keyword(keyword):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("DELETE FROM gaming_keywords WHERE keyword=?", (keyword.lower(),))
            await db.commit()

# --- Pending Management ---
async def save_pending(title, url, summary, image_url):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            try:
                await db.execute("INSERT INTO pending (title, url, summary, image_url, created_at) VALUES (?, ?, ?, ?, ?)",
                                 (title, url, summary, image_url, datetime.now().isoformat()))
                await db.commit()
                async with db.execute("SELECT last_insert_rowid()") as cursor:
                    row = await cursor.fetchone()
                    return row[0]
            except:
                return None

async def is_pending(url):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("SELECT id FROM pending WHERE url=?", (url,)) as cursor:
                result = await cursor.fetchone()
                return result is not None

async def get_pending(pending_id):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("SELECT * FROM pending WHERE id=?", (pending_id,)) as cursor:
                return await cursor.fetchone()

async def delete_pending(pending_id):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("DELETE FROM pending WHERE id=?", (pending_id,))
            await db.commit()

async def save_price_alert(watch_id, price, item_name, item_url):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("INSERT INTO price_alerts (watch_id, price, item_name, item_url) VALUES (?, ?, ?, ?)",
                             (watch_id, price, item_name, item_url))
            await db.commit()
    await log_event("price_alert", {"watch_id": watch_id, "price": price})

async def update_target_price(watch_id, new_price):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("UPDATE watches SET target_price=? WHERE id=?", (new_price, watch_id))
            await db.commit()

async def get_watch_info(watch_id):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("SELECT name, target_price FROM watches WHERE id=?", (watch_id,)) as cursor:
                return await cursor.fetchone()

async def cleanup_old_data():
    """Чистит старые записи чтобы не раздувать память и БД."""
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            # Логи здоровья старше 7 дней
            await db.execute("DELETE FROM events_log WHERE created_at < datetime('now', '-7 days')")
            # Pending новости старше 3 дней (уже не актуальны)
            await db.execute("DELETE FROM pending WHERE created_at < datetime('now', '-3 days')")
            # Posted старше 60 дней (для дедупликации достаточно 60 дней)
            await db.execute("DELETE FROM posted WHERE posted_at < datetime('now', '-60 days')")
            await db.commit()
            logger.info("Database cleanup completed.")
