import aiosqlite
import json
import logging
import asyncio
from datetime import datetime, timedelta
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

            # ТАБЛИЦЫ ДЛЯ ТОРГОВЛИ
            await db.execute("""
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
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS trade_state (
                    pair TEXT,
                    key TEXT,
                    value TEXT,
                    PRIMARY KEY (pair, key)
                )
            """)

            # ТАБЛИЦА ДЛЯ ВАКАНСИЙ
            await db.execute("""
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
            """)

            # ТАБЛИЦА ДЛЯ ДИНАМИЧЕСКИХ КОМПАНИЙ
            await db.execute("""
                CREATE TABLE IF NOT EXISTS target_companies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    url TEXT UNIQUE,
                    keywords TEXT, -- JSON list
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Миграция: добавляем новые колонки если их нет
            try:
                await db.execute("ALTER TABLE job_vacancies ADD COLUMN applied_at TIMESTAMP")
            except: pass
            try:
                await db.execute("ALTER TABLE job_vacancies ADD COLUMN follow_up_sent INTEGER DEFAULT 0")
            except: pass
            try:
                await db.execute("ALTER TABLE job_vacancies ADD COLUMN description TEXT")
            except: pass
            
            await db.commit()

async def log_event(event_type, meta=None):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            meta_json = json.dumps(meta) if meta else None
            await db.execute("INSERT INTO events_log (event_type, meta) VALUES (?, ?)", (event_type, meta_json))
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
            seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            async with db.execute("SELECT COUNT(*) FROM events_log WHERE event_type='post_approved' AND created_at > ?", (seven_days_ago,)) as cursor:
                approved = (await cursor.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM events_log WHERE event_type='post_rejected' AND created_at > ?", (seven_days_ago,)) as cursor:
                rejected = (await cursor.fetchone())[0]
            return {"approved": approved, "rejected": rejected}

async def set_oled_config(key: str, value: str):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("INSERT OR REPLACE INTO oled_config (key, value) VALUES (?, ?)", (key, value))
            await db.commit()

async def populate_initial_data(rss_feeds: list, keywords: list):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("SELECT COUNT(*) FROM rss_feeds") as cursor:
                if (await cursor.fetchone())[0] == 0:
                    for url in rss_feeds:
                        await db.execute("INSERT OR IGNORE INTO rss_feeds (url) VALUES (?)", (url,))
            async with db.execute("SELECT COUNT(*) FROM gaming_keywords") as cursor:
                if (await cursor.fetchone())[0] == 0:
                    for kw in keywords:
                        await db.execute("INSERT OR IGNORE INTO gaming_keywords (keyword) VALUES (?)", (kw.lower(),))
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

# --- Trading Functions ---
async def save_trade(pair, side, price, qty, pnl, signal, sentiment):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("""
                INSERT INTO trades (pair, side, price, qty, pnl, signal, sentiment) 
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (pair, side, price, qty, pnl, signal, sentiment))
            await db.commit()

def _decode_trade_state_value(val):
    """Поддерживает как новые JSON-значения, так и старые сырые строки из БД."""
    if val is None or not isinstance(val, str):
        return val

    raw = val.strip()
    if raw == "":
        return None

    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw

async def get_open_position(pair):
    """Возвращает текущую открытую позицию из trade_state для конкретной пары."""
    return await get_trade_state("current_position", pair)

async def get_trade_stats(days=7, pair=None):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            query = "SELECT COUNT(*), SUM(pnl) FROM trades WHERE created_at > datetime('now', ?)"
            params = [f'-{days} days']
            if pair:
                query += " AND pair = ?"
                params.append(pair)
            
            async with db.execute(query, params) as cursor:
                return await cursor.fetchone()

async def set_trade_state(key, val, pair="GLOBAL"):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            json_val = json.dumps(val)
            await db.execute("INSERT OR REPLACE INTO trade_state (pair, key, value) VALUES (?, ?, ?)", (pair, key, json_val))
            await db.commit()

async def get_trade_state(key, pair="GLOBAL"):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("SELECT value FROM trade_state WHERE pair=? AND key=?", (pair, key)) as cursor:
                row = await cursor.fetchone()
                return _decode_trade_state_value(row[0]) if row else None

# --- Job Hunter Functions ---
async def save_vacancy(title, company, url, salary_raw, is_remote, score, verdict, has_salary, description=""):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            try:
                await db.execute("""
                    INSERT INTO job_vacancies (title, company, url, salary_raw, is_remote, score, match_verdict, has_salary, description)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (title, company, url, salary_raw, is_remote, score, verdict, int(has_salary), description))
                await db.commit()
                return True
            except Exception as e:
                logger.error(f"Error saving vacancy: {e}")
                return False

async def get_vacancy_details(vacancy_id):
    """Возвращает полную информацию о вакансии для генерации письма."""
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("SELECT title, company, description, url FROM job_vacancies WHERE id = ?", (vacancy_id,)) as cursor:
                return await cursor.fetchone()

async def is_vacancy_seen(url):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("SELECT id FROM job_vacancies WHERE url=?", (url,)) as cursor:
                row = await cursor.fetchone()
                return row is not None

async def get_top_vacancies(limit=10, offset=0):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("""
                SELECT * FROM job_vacancies 
                WHERE dismissed = 0 
                ORDER BY score DESC, created_at DESC 
                LIMIT ? OFFSET ?
            """, (limit, offset)) as cursor:
                return await cursor.fetchall()

async def dismiss_vacancy(vacancy_id):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("UPDATE job_vacancies SET dismissed = 1 WHERE id = ?", (vacancy_id,))
            await db.commit()

async def mark_vacancy_applied(vacancy_id):
    """Помечает вакансию как откликнутую."""
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("""
                UPDATE job_vacancies 
                SET applied_at = CURRENT_TIMESTAMP, dismissed = 1 
                WHERE id = ?
            """, (vacancy_id,))
            await db.commit()

async def get_pending_follow_ups(days=7):
    """Возвращает отклики без напоминания, которым больше X дней."""
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("""
                SELECT id, title, company, url, applied_at FROM job_vacancies 
                WHERE applied_at IS NOT NULL 
                AND follow_up_sent = 0
                AND applied_at < datetime('now', ?)
            """, (f'-{days} days',)) as cursor:
                return await cursor.fetchall()

async def mark_follow_up_sent(vacancy_id):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("UPDATE job_vacancies SET follow_up_sent = 1 WHERE id = ?", (vacancy_id,))
            await db.commit()

async def get_applied_vacancies(limit=20):
    """Возвращает список вакансий, на которые был сделан отклик."""
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("""
                SELECT id, title, company, url, applied_at, follow_up_sent 
                FROM job_vacancies 
                WHERE applied_at IS NOT NULL 
                ORDER BY applied_at DESC 
                LIMIT ?
            """, (limit,)) as cursor:
                return await cursor.fetchall()

async def get_recent_job_history(applied_limit=3, dismissed_limit=3):
    """Возвращает краткую историю предпочтений (что понравилось, а что нет)."""
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            # Что понравилось (Applied)
            async with db.execute("""
                SELECT title, company, match_verdict FROM job_vacancies 
                WHERE applied_at IS NOT NULL 
                ORDER BY applied_at DESC LIMIT ?
            """, (applied_limit,)) as cursor:
                applied_rows = await cursor.fetchall()
            
            # Что НЕ понравилось (Dismissed и score < 6)
            async with db.execute("""
                SELECT title, company, match_verdict FROM job_vacancies 
                WHERE dismissed = 1 AND applied_at IS NULL AND score < 6
                ORDER BY created_at DESC LIMIT ?
            """, (dismissed_limit,)) as cursor:
                dismissed_rows = await cursor.fetchall()
                
            return {
                "liked": [{"title": r[0], "company": r[1], "reason": r[2]} for r in applied_rows],
                "disliked": [{"title": r[0], "company": r[1], "reason": r[2]} for r in dismissed_rows]
            }

# --- Dynamic Companies Management ---
async def add_target_company(name, url, keywords=None):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            kw_json = json.dumps(keywords if keywords else [])
            try:
                await db.execute("INSERT OR REPLACE INTO target_companies (name, url, keywords) VALUES (?, ?, ?)",
                                 (name, url, kw_json))
                await db.commit()
                return True
            except Exception as e:
                logger.error(f"Error adding target company: {e}")
                return False

async def get_target_companies():
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("SELECT name, url, keywords FROM target_companies") as cursor:
                rows = await cursor.fetchall()
                return [{"name": r[0], "url": r[1], "keywords": json.loads(r[2])} for r in rows]

async def remove_target_company(company_id):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("DELETE FROM target_companies WHERE id = ?", (company_id,))
            await db.commit()

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

async def get_recent_sentiments(hours=12):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            async with db.execute("""
                SELECT meta FROM events_log
                WHERE event_type='news_sentiment'
                AND created_at > datetime('now', ?)
            """, (f'-{hours} hours',)) as cursor:
                return await cursor.fetchall()

async def get_weekly_stats():
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            from datetime import datetime, timedelta
            seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            async with db.execute("SELECT COUNT(*) FROM events_log WHERE event_type='post_approved' AND created_at > ?", (seven_days_ago,)) as cursor:
                approved = (await cursor.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM events_log WHERE event_type='post_rejected' AND created_at > ?", (seven_days_ago,)) as cursor:
                rejected = (await cursor.fetchone())[0]
            return {"approved": approved, "rejected": rejected}

async def get_daily_trades(hours=24):
    async with db_lock:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            from datetime import datetime, timedelta
            time_ago = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
            async with db.execute("""
                SELECT pair, side, price, qty, pnl, signal, sentiment, created_at
                FROM trades
                WHERE created_at > ?
                ORDER BY created_at ASC
            """, (time_ago,)) as cursor:
                return await cursor.fetchall()
