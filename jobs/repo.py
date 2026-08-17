import json
import logging

from core.db import connect, normalize_job_url

logger = logging.getLogger(__name__)

_seen_vacancies_cache = None


async def load_seen_vacancies_cache():
    global _seen_vacancies_cache
    async with connect() as db:
        async with db.execute("SELECT url FROM job_vacancies") as cursor:
            rows = await cursor.fetchall()
            _seen_vacancies_cache = {row[0] for row in rows if row[0]}
    logger.info("Loaded %s seen vacancy URLs into memory cache.", len(_seen_vacancies_cache))


def clear_seen_vacancies_cache():
    global _seen_vacancies_cache
    _seen_vacancies_cache = None
    logger.info("Cleared seen vacancies cache.")


async def save_vacancy(title, company, url, salary_raw, is_remote, score, verdict, has_salary, description=""):
    url = normalize_job_url(url)
    async with connect(commit=True) as db:
        try:
            await db.execute(
                """
                INSERT INTO job_vacancies (title, company, url, salary_raw, is_remote, score, match_verdict, has_salary, description)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (title, company, url, salary_raw, is_remote, score, verdict, int(has_salary), description),
            )
            if _seen_vacancies_cache is not None:
                _seen_vacancies_cache.add(url)
            return True
        except Exception as e:
            logger.error("Error saving vacancy: %s", e)
            return False


async def save_vacancies_batch(vacancies):
    if not vacancies:
        return True
    normalized = []
    for v in vacancies:
        row = list(v)
        row[2] = normalize_job_url(row[2])
        normalized.append(tuple(row))
    async with connect(commit=True) as db:
        try:
            await db.executemany(
                """
                INSERT OR IGNORE INTO job_vacancies (title, company, url, salary_raw, is_remote, score, match_verdict, has_salary, description)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                normalized,
            )
            if _seen_vacancies_cache is not None:
                for v in normalized:
                    _seen_vacancies_cache.add(v[2])
            return True
        except Exception as e:
            logger.error("Error saving vacancies batch: %s", e)
            return False


async def get_vacancy_details(vacancy_id):
    async with connect() as db:
        async with db.execute(
            "SELECT title, company, description, url FROM job_vacancies WHERE id = ?",
            (vacancy_id,),
        ) as cursor:
            return await cursor.fetchone()


async def is_vacancy_seen(url):
    global _seen_vacancies_cache
    normalized = normalize_job_url(url)
    if not normalized:
        return False
    if _seen_vacancies_cache is not None:
        return normalized in _seen_vacancies_cache
    async with connect() as db:
        async with db.execute("SELECT id FROM job_vacancies WHERE url=?", (normalized,)) as cursor:
            return await cursor.fetchone() is not None


async def get_top_vacancies(limit=10, offset=0):
    async with connect() as db:
        async with db.execute(
            """
            SELECT * FROM job_vacancies
            WHERE dismissed = 0
            ORDER BY score DESC, created_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ) as cursor:
            return await cursor.fetchall()


async def get_dismissed_vacancies(limit=10, offset=0):
    async with connect() as db:
        async with db.execute(
            """
            SELECT * FROM job_vacancies
            WHERE dismissed = 1 AND applied_at IS NULL
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ) as cursor:
            return await cursor.fetchall()


async def get_job_stats():
    async with connect() as db:
        async with db.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN dismissed = 0 THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN dismissed = 1 AND applied_at IS NULL THEN 1 ELSE 0 END) AS dismissed,
                SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) AS applied,
                MAX(created_at) AS last_added
            FROM job_vacancies
            """
        ) as cursor:
            row = await cursor.fetchone()
        async with db.execute("SELECT COUNT(*) FROM job_raw WHERE status='pending'") as cursor:
            raw_pending = (await cursor.fetchone())[0] or 0
        async with db.execute("SELECT COUNT(*) FROM job_score_cache") as cursor:
            cache_size = (await cursor.fetchone())[0] or 0
        async with db.execute("SELECT value FROM trade_state WHERE key='job_search_query'") as cursor:
            query_row = await cursor.fetchone()
        return {
            "total": row[0] or 0,
            "active": row[1] or 0,
            "dismissed": row[2] or 0,
            "applied": row[3] or 0,
            "last_added": row[4],
            "raw_pending": raw_pending,
            "score_cache_size": cache_size,
            "search_query": query_row[0] if query_row else None,
        }


async def save_job_raw_batch(jobs):
    if not jobs:
        return 0
    rows = []
    for j in jobs:
        url = normalize_job_url(j.get("url"))
        if not url:
            continue
        rows.append((
            j.get("title", ""),
            j.get("company", ""),
            url,
            j.get("salary_raw", "See website"),
            1 if j.get("is_remote", True) else 0,
            j.get("source", ""),
            j.get("description", ""),
        ))
    if not rows:
        return 0
    async with connect(commit=True) as db:
        async with db.execute("SELECT COUNT(*) FROM job_raw WHERE status='pending'") as cursor:
            before = (await cursor.fetchone())[0]
        await db.executemany(
            """
            INSERT OR IGNORE INTO job_raw
            (title, company, url, salary_raw, is_remote, source, description, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            rows,
        )
        async with db.execute("SELECT COUNT(*) FROM job_raw WHERE status='pending'") as cursor:
            after = (await cursor.fetchone())[0]
        return after - before


async def get_pending_raw_jobs(limit=50):
    async with connect() as db:
        async with db.execute(
            """
            SELECT title, company, url, salary_raw, is_remote, source, description
            FROM job_raw
            WHERE status = 'pending'
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {
            "title": r[0], "company": r[1], "url": r[2],
            "salary_raw": r[3], "is_remote": bool(r[4]),
            "source": r[5], "description": r[6] or "",
        }
        for r in rows
    ]


async def mark_raw_jobs_scored(urls):
    if not urls:
        return
    normalized = [normalize_job_url(u) for u in urls if normalize_job_url(u)]
    if not normalized:
        return
    async with connect(commit=True) as db:
        placeholders = ",".join("?" * len(normalized))
        await db.execute(
            f"UPDATE job_raw SET status='scored' WHERE url IN ({placeholders})",
            normalized,
        )


async def get_cached_job_score(url):
    normalized = normalize_job_url(url)
    if not normalized:
        return None
    async with connect() as db:
        async with db.execute(
            """
            SELECT score, is_worldwide, core_stack_match, matching_skills,
                   missing_skills, location_reason, verdict, has_salary, scoring_mode
            FROM job_score_cache WHERE url = ?
            """,
            (normalized,),
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return None
    return {
        "score": row[0],
        "is_worldwide": bool(row[1]),
        "core_stack_match": bool(row[2]),
        "matching_skills": json.loads(row[3] or "[]"),
        "missing_skills": json.loads(row[4] or "[]"),
        "location_reason": row[5] or "",
        "verdict": row[6] or "",
        "has_salary": bool(row[7]),
        "scoring_mode": row[8] or "cache",
    }


async def save_job_score_cache(url, result):
    normalized = normalize_job_url(url)
    if not normalized:
        return
    async with connect(commit=True) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO job_score_cache
            (url, score, is_worldwide, core_stack_match, matching_skills,
             missing_skills, location_reason, verdict, has_salary, scoring_mode, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                normalized,
                int(result.get("score", 0)),
                int(bool(result.get("is_worldwide", False))),
                int(bool(result.get("core_stack_match", False))),
                json.dumps(result.get("matching_skills", [])),
                json.dumps(result.get("missing_skills", [])),
                str(result.get("location_reason", "")),
                str(result.get("verdict", "")),
                int(bool(result.get("has_salary", False))),
                result.get("scoring_mode", "unknown"),
            ),
        )


async def dismiss_vacancy(vacancy_id):
    async with connect(commit=True) as db:
        await db.execute("UPDATE job_vacancies SET dismissed = 1 WHERE id = ?", (vacancy_id,))


async def mark_vacancy_applied(vacancy_id):
    async with connect(commit=True) as db:
        await db.execute(
            """
            UPDATE job_vacancies
            SET applied_at = CURRENT_TIMESTAMP, dismissed = 1
            WHERE id = ?
            """,
            (vacancy_id,),
        )


async def get_pending_follow_ups(days=7):
    async with connect() as db:
        async with db.execute(
            """
            SELECT id, title, company, url, applied_at FROM job_vacancies
            WHERE applied_at IS NOT NULL
            AND follow_up_sent = 0
            AND applied_at < datetime('now', ?)
            """,
            (f"-{days} days",),
        ) as cursor:
            return await cursor.fetchall()


async def mark_follow_up_sent(vacancy_id):
    async with connect(commit=True) as db:
        await db.execute(
            "UPDATE job_vacancies SET follow_up_sent = 1 WHERE id = ?",
            (vacancy_id,),
        )


async def get_applied_vacancies(limit=20):
    async with connect() as db:
        async with db.execute(
            """
            SELECT id, title, company, url, applied_at, follow_up_sent
            FROM job_vacancies
            WHERE applied_at IS NOT NULL
            ORDER BY applied_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            return await cursor.fetchall()


async def get_recent_job_history(applied_limit=3, dismissed_limit=3):
    async with connect() as db:
        async with db.execute(
            """
            SELECT title, company, match_verdict FROM job_vacancies
            WHERE applied_at IS NOT NULL
            ORDER BY applied_at DESC LIMIT ?
            """,
            (applied_limit,),
        ) as cursor:
            applied_rows = await cursor.fetchall()
        async with db.execute(
            """
            SELECT title, company, match_verdict FROM job_vacancies
            WHERE dismissed = 1 AND applied_at IS NULL AND score < 6
            ORDER BY created_at DESC LIMIT ?
            """,
            (dismissed_limit,),
        ) as cursor:
            dismissed_rows = await cursor.fetchall()
        return {
            "liked": [{"title": r[0], "company": r[1], "reason": r[2]} for r in applied_rows],
            "disliked": [{"title": r[0], "company": r[1], "reason": r[2]} for r in dismissed_rows],
        }


async def add_target_company(name, url, keywords=None):
    async with connect(commit=True) as db:
        kw_json = json.dumps(keywords if keywords else [])
        try:
            await db.execute(
                "INSERT OR REPLACE INTO target_companies (name, url, keywords) VALUES (?, ?, ?)",
                (name, url, kw_json),
            )
            return True
        except Exception as e:
            logger.error("Error adding target company: %s", e)
            return False


async def get_target_companies():
    async with connect() as db:
        async with db.execute("SELECT name, url, keywords FROM target_companies") as cursor:
            rows = await cursor.fetchall()
            return [{"name": r[0], "url": r[1], "keywords": json.loads(r[2])} for r in rows]


async def remove_target_company(company_id):
    async with connect(commit=True) as db:
        await db.execute("DELETE FROM target_companies WHERE id = ?", (company_id,))
