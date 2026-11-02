import gc
import html
import logging
from datetime import datetime, timezone

from telegram.ext import ContextTypes

from ai.jobs import process_jobs_scoring_batch
from ai.job_role_filters import passes_title_filter, require_frontend_title
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

from trade.repo import set_trade_state

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


def _format_job_verdict(result, source, location=""):
    loc_prefix = f"📍 {location}" if location else ("🌍 Worldwide" if result["is_worldwide"] else "📍 Restricted")
    matching = ", ".join(result.get("matching_skills", [])[:4]) or "не указаны"
    missing = ", ".join(result.get("missing_skills", [])[:4]) or "нет критичных пробелов"
    return (
        f"[{source}] [{loc_prefix}] {result['verdict']}. "
        f"Совпадения: {matching}. Пробелы: {missing}."
    )


def _job_passes_filters(result, title: str = ""):
    if title and not passes_title_filter(title):
        return False
    if result["score"] < JOB_MIN_SCORE:
        return False
    if JOB_REQUIRE_WORLDWIDE and not result["is_worldwide"]:
        return False
    # Title уже прошёл passes_title_filter (frontend-роль); core_stack_match модели учитываем только
    # для эвристики — модель часто ставит false, когда стек просто не указан.
    if result.get("scoring_mode") == "rules" and not result.get("core_stack_match", False):
        return False
    return True


async def _score_pending_queue(history, status_text="", raw_added=0, fetch_count=0, source_stats=None):
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
                if not _job_passes_filters(result, v.get("title", "")):
                    continue
                mode_tag = {
                    "groq": "Groq", "gemini": "Gemini",
                    "local": "local/qwen3.5-coder",
                    "rules": "эвристика", "cache": "кеш",
                }.get(mode, mode)
                full_verdict = _format_job_verdict(result, f"{v.get('source', 'Unknown')} | {mode_tag}",
                                                   v.get("location", ""))
                passed_vacancies.append((
                    v["title"], v["company"], v["url"], v["salary_raw"],
                    1 if v.get("is_remote", True) else 0,
                    result["score"], full_verdict,
                    1 if result["has_salary"] else 0,
                    v.get("description", ""),
                    v.get("location", ""),
                ))
        except Exception as e:
            logger.error(f"Error scoring batch at offset {i}: {e}")

    await mark_raw_jobs_scored(scored_urls)

    new_count = 0
    if passed_vacancies and await save_vacancies_batch(passed_vacancies):
        new_count = len(passed_vacancies)

    stats = await get_job_stats()
    mode_summary = ", ".join(f"{k}: {v}" for k, v in sorted(scoring_modes.items())) or "нет"
    top = sorted(passed_vacancies, key=lambda x: -x[5])[:5]
    top_lines = "\n".join(
        f"• <b>{x[5]}/10</b> <a href=\"{html.escape(x[2])}\">{html.escape(x[0][:70])}</a> — "
        f"{html.escape((x[1] or '—')[:30])}, <i>{html.escape((x[9] or '')[:40])}</i>"
        for x in top
    )
    src_line = ""
    if source_stats:
        src_line = "\n<b>Источники:</b> " + " · ".join(
            f"{s['name']} {'⚠️' if s.get('error') else ''}{s['relevant']}" for s in source_stats
        ) + "\n"
    return new_count, (
        f"💼 <b>Поиск завершен</b>\n\n"
        f"✅ Новых подходящих: <b>{new_count}</b>\n"
        + (f"{top_lines}\n" if top_lines else "")
        + f"{src_line}"
        f"Проверено моделью: {len(target_vacancies)} | В очереди: <b>{stats['raw_pending']}</b>\n"
        f"Новых после фильтров: {fetch_count} | +в очередь: {raw_added}\n"
        f"Скоринг: <code>{mode_summary}</code>\n\n"
        f"Список: /jobs"
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
            vacancies, source_stats = await fetch_all_jobs(progress_callback=update_progress if message else None)
            await set_trade_state("job_sources_last", {
                "at": datetime.now(timezone.utc).isoformat(), "sources": source_stats})
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
            source_stats=source_stats,
        )
        
        if message:
            await message.edit_text(final_msg, parse_mode="HTML", disable_web_page_preview=True)
        else:
            await context.bot.send_message(chat_id=ADMIN_ID, text=final_msg, parse_mode="HTML",
                                           disable_web_page_preview=True)
            
    finally:
        # Обязательно очищаем кеш в памяти и вызываем GC
        clear_seen_vacancies_cache()
        import gc
        gc.collect()
