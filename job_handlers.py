import asyncio
import html
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import (
    save_vacancy, get_top_vacancies, dismiss_vacancy, is_vacancy_seen,
    get_trade_state, set_trade_state
)
from job_fetcher import fetch_all_jobs
from ai_utils import process_job_scoring
from config import ADMIN_ID, JOB_MIN_SCORE, JOB_REQUIRE_WORLDWIDE

logger = logging.getLogger(__name__)

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

async def job_fetch_job(context: ContextTypes.DEFAULT_TYPE):
    """Периодическая задача по поиску вакансий."""
    logger.info("Starting job hunting...")
    vacancies = await fetch_all_jobs()
    
    count = 0
    for v in vacancies:
        if await is_vacancy_seen(v["url"]): continue
        
        # Gemini скоринг с учетом Worldwide Remote
        result = await process_job_scoring(v["title"], v["company"], v.get("description", ""))
        
        if _job_passes_filters(result):
            full_verdict = _format_job_verdict(result, v.get("source", "Unknown"))

            await save_vacancy(
                v["title"], v["company"], v["url"], v["salary_raw"],
                v["is_remote"], result["score"], full_verdict, result["has_salary"]
            )
            count += 1
            await asyncio.sleep(1) # Небольшая пауза для API Gemini
            
    if count > 0:
        await context.bot.send_message(
            chat_id=ADMIN_ID, 
            text=(
                f"💼 <b>Международный поиск: +{count} новых вакансий!</b>\n"
                f"Фильтр: score >= {JOB_MIN_SCORE}, core stack match, "
                f"{'worldwide only' if JOB_REQUIRE_WORLDWIDE else 'remote allowed'}.\n"
                f"Используйте команду /jobs."
            ),
            parse_mode="HTML"
        )
    else:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text="💤 Поиск завершен, но новых подходящих вакансий не найдено.",
            parse_mode="HTML",
        )

async def jobs_refresh_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной запуск поиска вакансий."""
    await update.message.reply_text("🔎 Запускаю поиск вакансий...")
    await job_fetch_job(context)
    await update.message.reply_text("✅ Поиск завершен. Используйте /jobs.")

async def list_jobs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выводит топ подходящих вакансий."""
    target_message = update.callback_query.message if update.callback_query else update.message
    vacancies = await get_top_vacancies(limit=5)
    
    if not vacancies:
        await target_message.reply_text("💤 Пока новых подходящих вакансий не найдено.")
        return

    for v in vacancies:
        vid, title, company, url, salary, remote, score, verdict, has_salary, _, _ = v
        
        match_emoji = "🟢" if score >= 8 else "🟡" if score >= 6 else "🔴"
        
        text = (
            f"{match_emoji} <b>{html.escape(title)}</b>\n"
            f"🏢 {html.escape(company)}\n"
            f"💰 {html.escape(salary)}\n"
            f"📊 Оценка: <b>{score}/10</b>\n\n"
            f"📝 <i>{html.escape(verdict)}</i>\n\n"
            f"🔗 {html.escape(url)}"
        )
        
        keyboard = [[InlineKeyboardButton("❌ Пропустить", callback_data=f"dismiss_job_{vid}")]]
        
        await target_message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def dismiss_job_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    vid = int(query.data.split("_")[-1])
    await dismiss_vacancy(vid)
    await query.message.edit_text("📁 Вакансия перенесена в архив.")

async def job_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Позволяет просматривать и менять поисковый запрос для вакансий."""
    if not context.args:
        current = await get_trade_state("job_search_query")
        current = current if current else "Vue TypeScript Frontend (по умолчанию)"
        await update.message.reply_text(
            f"🔎 <b>Текущий поисковый запрос:</b>\n<code>{html.escape(current)}</code>\n\n"
            f"Чтобы изменить, напишите: <code>/job_query React Senior</code>",
            parse_mode="HTML"
        )
        return

    new_query = " ".join(context.args)
    await set_trade_state("job_search_query", new_query)
    await update.message.reply_text(
        f"✅ <b>Запрос изменен!</b>\nТеперь бот ищет: <code>{html.escape(new_query)}</code>",
        parse_mode="HTML"
    )
