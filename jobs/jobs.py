import gc
import html
import logging

from telegram.ext import ContextTypes

from ai.jobs import process_jobs_scoring_batch
from config import (
    ADMIN_ID,
    JOB_AI_BATCH_SIZE,
    JOB_AI_SCORE_LIMIT,
    JOB_MIN_SCORE,
    JOB_RAW_QUEUE_LIMIT,
    JOB_REQUIRE_WORLDWIDE,
)
from jobs.fetcher import fetch_all_jobs
from jobs.repo import (
    clear_seen_vacancies_cache,
    get_job_stats,
    get_pending_follow_ups,
    get_pending_raw_jobs,
    get_recent_job_history,
    is_vacancy_seen,
    load_seen_vacancies_cache,
    mark_follow_up_sent,
    mark_raw_jobs_scored,
    save_job_raw_batch,
    save_vacancies_batch,
)

logger = logging.getLogger(__name__)


async def check_job_follow_ups(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет вакансии, на которые вы откликнулись 7 дней назад."""
    pending = await get_pending_follow_ups(days=7)
    if not pending:
        return

    for vid, title, company, url, applied_at in pending:
        text = (
            f"🔔 <b>Пора спросить фидбек!</b>\n\n"
            f"Прошло 7 дней с вашего отклика на вакансию:\n"
            f"📦 <b>{html.escape(title)}</b>\n"
            f"🏢 {html.escape(company)}\n"
            f"📅 Дата отклика: {applied_at[:10]}\n\n"
            f"🔗 {html.escape(url)}"
        )
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
            await mark_follow_up_sent(vid)
        except Exception as e:
            logger.error("Error sending follow-up for %s: %s", vid, e)


def get_vacancy_priority(v):
    title = v.get("title", "").lower()
    desc = v.get("description", "").lower()
    if "vue" in title or "nuxt" in title or "composition api" in title:
        return 0
    if "vue 3" in desc or "nuxt" in desc or "composition api" in desc:
        return 1
    if "react" in title or "next" in title or "typescript" in title:
        return 2
    if "vue" in desc:
        return 3
    if "react" in desc or "typescript" in desc:
        return 4
    if "frontend" in title or "front-end" in title or "javascript" in title:
        return 5
    return 6


def _normalize_search_query(query: str) -> str:
    return query.strip().strip('"').strip("'") if query else query


def _format_job_verdict(result, source):
    loc_prefix = "🌍 Worldwide" if result["is_worldwide"] else "📍 Restricted"
    matching = ", ".join(result.get("matching_skills", [])[:4]) or "не указаны"
    missing = ", ".join(result.get("missing_skills", [])[:4]) or "нет критичных пробелов"
    return (
        f"[{source}] [{loc_prefix}] {result['location_reason']}. {result['verdict']}. "
        f"Совпадения: {matching}. Пробелы: {missing}."
    )


def _job_passes_filters(result):
    if result["score"] < JOB_MIN_SCORE:
        return False
    if JOB_REQUIRE_WORLDWIDE and not result["is_worldwide"]:
        return False
    if not result.get("core_stack_match", False):
        return False
    return True


async def _score_pending_queue(history, status_text="", raw_added=0, fetch_count=0):
    pending_raw = await get_pending_raw_jobs(limit=JOB_AI_SCORE_LIMIT)
    pending_raw = sorted(pending_raw, key=get_vacancy_priority)
    target_vacancies = pending_raw[:JOB_AI_SCORE_LIMIT]

    passed_vacancies = []
    scoring_modes = {}
    scored_urls = []

    for i in range(0, len(target_vacancies), JOB_AI_BATCH_SIZE):
        batch = target_vacancies[i:i + JOB_AI_BATCH_SIZE]
        try:
            results = await process_jobs_scoring_batch(batch, history=history)
            for v, result in zip(batch, results):
                scored_urls.append(v["url"])
                mode = result.get("scoring_mode", "unknown")
                scoring_modes[mode] = scoring_modes.get(mode, 0) + 1
                if not _job_passes_filters(result):
                    continue
                mode_tag = {
                    "groq": "Groq", "gemini": "Gemini",
                    "local": "local/qwen3.5-coder",
                    "rules": "эвристика", "cache": "кеш",
                }.get(mode, mode)
                full_verdict = _format_job_verdict(result, f"{v.get('source', 'Unknown')} | {mode_tag}")
                passed_vacancies.append((
                    v["title"], v["company"], v["url"], v["salary_raw"],
                    1 if v.get("is_remote", True) else 0,
                    result["score"], full_verdict,
                    1 if result["has_salary"] else 0,
                    v.get("description", ""),
                ))
        except Exception as e:
            logger.error(f"Error scoring batch at offset {i}: {e}")

    await mark_raw_jobs_scored(scored_urls)

    new_count = 0
    if passed_vacancies and await save_vacancies_batch(passed_vacancies):
        new_count = len(passed_vacancies)

    stats = await get_job_stats()
    mode_summary = ", ".join(f"{k}: {v}" for k, v in sorted(scoring_modes.items())) or "нет"
    return new_count, (
        f"💼 <b>Поиск завершен!</b>\n\n"
        f"✅ Отобрано новых подходящих: <b>{new_count}</b>\n"
        f"Проверено: {len(target_vacancies)} | В очереди: <b>{stats['raw_pending']}</b>\n"
        f"Найдено в fetch: {fetch_count} | +в очередь: {raw_added}\n"
        f"Скоринг: <code>{mode_summary}</code>\n\n"
        f"Используйте /jobs или кнопку Список."
    )


async def job_fetch_job(context: ContextTypes.DEFAULT_TYPE, message=None):
    """Периодическая задача по поиску вакансий с поддержкой параллельного скоринга."""
    logger.info("Starting job hunting...")
    
    # Загружаем кеш просмотренных вакансий
    await load_seen_vacancies_cache()
    
    try:
        status_text = "🔎 <b>Поиск вакансий...</b>\n\n"
        
        async def update_progress(current, total, source_name):
            nonlocal status_text
            percent = int((current / total) * 100)
            bar = "🟢" * (current // 2) + "⚪️" * ((total - current) // 2)
            new_text = f"{status_text}Прогресс: {bar} {percent}%\nСейчас: <code>{source_name}</code>"
            if message:
                try:
                    await message.edit_text(new_text, parse_mode="HTML")
                except:
                    pass

        try:
            vacancies = await fetch_all_jobs(progress_callback=update_progress if message else None)
        except Exception as e:
            logger.exception("Job fetch failed")
            err_msg = f"❌ <b>Ошибка поиска:</b> <code>{html.escape(str(e)[:200])}</code>"
            if message:
                await message.edit_text(err_msg, parse_mode="HTML")
            else:
                await context.bot.send_message(chat_id=ADMIN_ID, text=err_msg, parse_mode="HTML")
            return

        unseen_vacancies = []
        if vacancies:
            for v in vacancies:
                if not await is_vacancy_seen(v["url"]):
                    unseen_vacancies.append(v)

        raw_added = await save_job_raw_batch(unseen_vacancies[:JOB_RAW_QUEUE_LIMIT])
        history = await get_recent_job_history(3, 3)

        if message:
            await message.edit_text(
                f"{status_text}🧠 <b>Оцениваю очередь вакансий...</b>\n"
                f"Новых в fetch: {len(unseen_vacancies)} | +в очередь: {raw_added}",
                parse_mode="HTML",
            )

        _, final_msg = await _score_pending_queue(
            history,
            raw_added=raw_added,
            fetch_count=len(unseen_vacancies),
        )
        
        if message:
            await message.edit_text(final_msg, parse_mode="HTML")
        else:
            await context.bot.send_message(chat_id=ADMIN_ID, text=final_msg, parse_mode="HTML")
            
    finally:
        # Обязательно очищаем кеш в памяти и вызываем GC
        clear_seen_vacancies_cache()
        import gc
        gc.collect()
