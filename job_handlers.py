import asyncio
import html
import logging
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import (
    save_vacancy, get_top_vacancies, get_dismissed_vacancies, get_job_stats,
    dismiss_vacancy, is_vacancy_seen,
    get_trade_state, set_trade_state, get_recent_job_history,
    load_seen_vacancies_cache, clear_seen_vacancies_cache, save_vacancies_batch,
    save_job_raw_batch, get_pending_raw_jobs, mark_raw_jobs_scored,
)
from job_fetcher import fetch_all_jobs
from ai.jobs import process_jobs_scoring_batch, suggest_new_companies
from config import (
    ADMIN_ID, JOB_MIN_SCORE, JOB_REQUIRE_WORLDWIDE,
    JOB_AI_SCORE_LIMIT, JOB_AI_BATCH_SIZE, JOB_RAW_QUEUE_LIMIT,
)

logger = logging.getLogger(__name__)


def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id != ADMIN_ID:
            return
        return await func(update, context, *args, **kwargs)
    return wrapped


async def _clear_job_list_messages(context, chat_id):
    for mid in context.user_data.pop("job_message_ids", []):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass


def _format_vacancy_card(v, show_actions=True):
    vid, title, company, url, salary, score, verdict = v[0], v[1], v[2], v[3], v[4], v[6], v[7]
    match_emoji = "🟢" if score >= 8 else "🟡" if score >= 6 else "🔴"
    text = (
        f"{match_emoji} <b>{html.escape(title)}</b>\n"
        f"🏢 {html.escape(company)}\n"
        f"💰 {html.escape(salary)}\n"
        f"📊 Оценка: <b>{score}/10</b>\n\n"
        f"📝 <i>{html.escape(verdict[:500])}</i>\n\n"
        f"🔗 {html.escape(url)}"
    )
    keyboard = None
    if show_actions:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔥 ГЕНЕРИРОВАТЬ ПИСЬМО", callback_data=f"cover_job_{vid}")],
            [InlineKeyboardButton("✅ Я откликнулся", callback_data=f"apply_job_{vid}"),
             InlineKeyboardButton("❌ Пропустить", callback_data=f"dismiss_job_{vid}")],
        ])
    return text, keyboard

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

@admin_only
async def jobs_refresh_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной запуск поиска вакансий с прогресс-баром."""
    target_msg = update.effective_message
    status_msg = await target_msg.reply_text("🔎 Подготовка к поиску...")
    await job_fetch_job(context, message=status_msg)

@admin_only
async def list_jobs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выводит топ подходящих вакансий с пагинацией."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    offset = 0
    if query and (query.data == "jobs_list" or query.data.startswith("jobs_list_")):
        if query.data.startswith("jobs_list_"):
            offset = int(query.data.split("_")[-1])
        await _clear_job_list_messages(context, chat_id)

    limit = 5
    vacancies = await get_top_vacancies(limit=limit + 1, offset=offset)

    if not vacancies and offset == 0:
        text = "💤 Пока новых подходящих вакансий не найдено."
        if query:
            try:
                await query.message.edit_text(text)
            except Exception:
                await context.bot.send_message(chat_id, text)
        else:
            await update.effective_message.reply_text(text)
        return
    if not vacancies:
        if query:
            await query.answer("Это все доступные вакансии.", show_alert=True)
        return

    has_more = len(vacancies) > limit
    display_vacancies = vacancies[:limit]
    message_ids = []

    for v in display_vacancies:
        text, keyboard = _format_vacancy_card(v)
        try:
            msg = await context.bot.send_message(
                chat_id, text, parse_mode="HTML", reply_markup=keyboard
            )
            message_ids.append(msg.message_id)
        except Exception as e:
            logger.error(f"Error sending job message: {e}")

    nav_buttons = []
    if offset > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"jobs_list_{max(0, offset - limit)}"))
    if has_more:
        nav_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"jobs_list_{offset + limit}"))

    if nav_buttons:
        nav = await context.bot.send_message(
            chat_id,
            f"Навигация (показано {offset + 1}-{offset + len(display_vacancies)}):",
            reply_markup=InlineKeyboardMarkup([nav_buttons]),
        )
        message_ids.append(nav.message_id)

    context.user_data["job_message_ids"] = message_ids


@admin_only
async def list_archive_jobs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    offset = 0
    if query and query.data.startswith("jobs_archive"):
        if query.data.startswith("jobs_archive_"):
            offset = int(query.data.split("_")[-1])
        await _clear_job_list_messages(context, chat_id)

    limit = 5
    vacancies = await get_dismissed_vacancies(limit=limit + 1, offset=offset)

    if not vacancies and offset == 0:
        text = "📁 Архив пуст."
        if query:
            try:
                await query.message.edit_text(text)
            except Exception:
                await context.bot.send_message(chat_id, text)
        else:
            await update.effective_message.reply_text(text)
        return
    if not vacancies:
        if query:
            await query.answer("Это весь архив.", show_alert=True)
        return

    has_more = len(vacancies) > limit
    display_vacancies = vacancies[:limit]
    message_ids = []

    for v in display_vacancies:
        text, _ = _format_vacancy_card(v, show_actions=False)
        msg = await context.bot.send_message(chat_id, text, parse_mode="HTML")
        message_ids.append(msg.message_id)

    nav_buttons = []
    if offset > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"jobs_archive_{max(0, offset - limit)}"))
    if has_more:
        nav_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"jobs_archive_{offset + limit}"))

    if nav_buttons:
        nav = await context.bot.send_message(
            chat_id,
            f"📁 Архив ({offset + 1}-{offset + len(display_vacancies)}):",
            reply_markup=InlineKeyboardMarkup([nav_buttons]),
        )
        message_ids.append(nav.message_id)

    context.user_data["job_message_ids"] = message_ids


@admin_only
async def jobs_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = await get_job_stats()
    query = await get_trade_state("job_search_query")
    ai_status = ["local/qwen3.5-coder"]

    last = stats["last_added"] or "никогда"
    text = (
        "📊 <b>Статистика вакансий</b>\n\n"
        f"Всего в базе: <b>{stats['total']}</b>\n"
        f"Активных: <b>{stats['active']}</b>\n"
        f"В архиве: <b>{stats['dismissed']}</b>\n"
        f"Откликов: <b>{stats['applied']}</b>\n"
        f"В очереди (raw): <b>{stats.get('raw_pending', 0)}</b>\n"
        f"Кеш скоринга: <b>{stats.get('score_cache_size', 0)}</b>\n"
        f"Последнее добавление: <code>{html.escape(str(last))}</code>\n"
        f"Запрос: <code>{html.escape(query or stats['search_query'] or 'Vue TypeScript Frontend')}</code>\n"
        f"ИИ: <code>{', '.join(ai_status)}</code>\n"
        f"Мин. оценка: <b>{JOB_MIN_SCORE}</b>"
    )
    await update.effective_message.reply_text(text, parse_mode="HTML")


@admin_only
async def cover_letter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерирует и отправляет полный пакет для отклика."""
    query = update.callback_query
    await query.answer("✍️ Формирую пакет для отклика...")
    
    vid = int(query.data.split("_")[-1])
    from database import get_vacancy_details
    from ai.jobs import generate_cover_letter
    from config import RESUME_URL
    
    details = await get_vacancy_details(vid)
    if not details:
        await query.message.reply_text("❌ Данные вакансии не найдены в базе.")
        return
        
    title, company, description, url = details[0], details[1], details[2], details[3]
    if not description:
        await query.message.reply_text("❌ Описание вакансии пустое, не могу составить письмо.")
        return
        
    letter = await generate_cover_letter(title, company, description)
    
    packet = (
        f"📋 <b>Пакет для отклика: {html.escape(company)}</b>\n\n"
        f"🔗 <b>Вакансия:</b> {html.escape(url)}\n"
        f"📄 <b>Резюме:</b> {html.escape(RESUME_URL)}\n\n"
        f"✉️ <b>Сопроводительное письмо (нажми, чтобы скопировать):</b>\n"
        f"<code>{html.escape(letter)}</code>"
    )
    
    await query.message.reply_text(packet, parse_mode="HTML")

@admin_only
async def list_applied_jobs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список вакансий, на которые вы уже откликнулись."""
    target_message = update.effective_message
    from database import get_applied_vacancies
    applied = await get_applied_vacancies(limit=10)
    
    if not applied:
        await target_message.reply_text("📝 Вы еще не пометили ни одну вакансию как откликнутую.")
        return

    text = "📂 <b>Ваши отклики (последние 10):</b>\n\n"
    for vid, title, company, url, applied_at, follow_up_sent in applied:
        status = "🔔 Напомню" if not follow_up_sent else "✅ Напомнил"
        date = applied_at[:10] if applied_at else "N/A"
        text += (
            f"🔹 <b>{html.escape(title)}</b> @ {html.escape(company)}\n"
            f"📅 {date} | {status}\n"
            f"🔗 {html.escape(url)}\n\n"
        )
    
    await target_message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

@admin_only
async def dismiss_job_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    vid = int(data.split("_")[-1])
    
    if data.startswith("apply_job_"):
        from database import mark_vacancy_applied
        await mark_vacancy_applied(vid)
        await query.message.edit_text("✅ Отлично! Я напомню написать им через 7 дней.")
    else:
        await dismiss_vacancy(vid)
        await query.message.edit_text("📁 Вакансия перенесена в архив.")

@admin_only
async def job_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Позволяет просматривать и менять поисковый запрос для вакансий."""
    target_msg = update.effective_message
    if not context.args:
        current = await get_trade_state("job_search_query")
        current = current if current else "Vue TypeScript Frontend (по умолчанию)"
        await target_msg.reply_text(
            f"🔎 <b>Текущий поисковый запрос:</b>\n<code>{html.escape(current)}</code>\n\n"
            f"Чтобы изменить, напишите: <code>/job_query React Middle/Senior</code>",
            parse_mode="HTML"
        )
        return

    new_query = _normalize_search_query(" ".join(context.args))
    await set_trade_state("job_search_query", new_query)
    await target_msg.reply_text(
        f"✅ <b>Запрос изменен!</b>\nТеперь бот ищет: <code>{html.escape(new_query)}</code>",
        parse_mode="HTML"
    )

@admin_only
async def job_discovery_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поиск и добавление новых компаний через локальную модель."""
    target_msg = update.effective_message
    if not context.args:
        await target_msg.reply_text(
            "🔎 <b>Discovery Mode</b>\n\n"
            "Напишите, какие компании искать. Например:\n"
            "<code>/discovery Финтех компании СНГ похожие на Каспи</code>\n"
            "<code>/discovery Стартапы в Европе с фокусом на AI и React</code>",
            parse_mode="HTML"
        )
        return

    prompt = " ".join(context.args)
    status_msg = await target_msg.reply_text("🤖 local/qwen3.5-coder ищет компании и прямые ссылки на вакансии...")
    
    added_count, companies = await suggest_new_companies(prompt)
    
    if added_count > 0:
        names = ", ".join([c['name'] for c in companies])
        text = (
            f"✅ <b>Найдено и добавлено компаний: {added_count}</b>\n\n"
            f"Список: <i>{html.escape(names)}</i>\n\n"
            f"Теперь при каждом поиске (раз в день или через /jobs_refresh) "
            f"я буду проверять их карьерные страницы."
        )
    else:
        text = "😔 К сожалению, не удалось найти новые подходящие компании или они уже есть в списке."
        
    await status_msg.edit_text(text, parse_mode="HTML")
