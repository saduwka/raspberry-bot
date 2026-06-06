import asyncio
import html
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import (
    save_vacancy, get_top_vacancies, dismiss_vacancy, is_vacancy_seen,
    get_trade_state, set_trade_state, get_recent_job_history,
    load_seen_vacancies_cache, clear_seen_vacancies_cache, save_vacancies_batch
)
from job_fetcher import fetch_all_jobs
from ai.jobs import process_job_scoring, suggest_new_companies
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

        vacancies = await fetch_all_jobs(progress_callback=update_progress if message else None)
        
        if not vacancies:
            final_msg = "💼 <b>Поиск завершен!</b>\n\nНовых вакансий не найдено."
            if message: await message.edit_text(final_msg, parse_mode="HTML")
            else: await context.bot.send_message(chat_id=ADMIN_ID, text=final_msg, parse_mode="HTML")
            return

        # 1. Отсеиваем уже просмотренные вакансии через быстрый кеш в ОЗУ
        unseen_vacancies = []
        for v in vacancies:
            if not await is_vacancy_seen(v["url"]):
                unseen_vacancies.append(v)

        if not unseen_vacancies:
            final_msg = "💼 <b>Поиск завершен!</b>\n\nНовых уникальных вакансий не найдено."
            if message: await message.edit_text(final_msg, parse_mode="HTML")
            else: await context.bot.send_message(chat_id=ADMIN_ID, text=final_msg, parse_mode="HTML")
            return

        # 2. Приоритизируем вакансии по стеку
        def get_vacancy_priority(v):
            title = v.get("title", "").lower()
            desc = v.get("description", "").lower()
            
            # Приоритет 1: Vue 3 / Composition API
            if "vue" in title or "composition api" in title:
                return 0
            # Приоритет 2: React / Next / TS
            if "react" in title or "next" in title or "typescript" in title or "ts" in title:
                return 1
            # Приоритет 3: Vue в описании
            if "vue" in desc:
                return 2
            # Приоритет 4: React / TS в описании
            if "react" in desc or "typescript" in desc or "ts" in desc:
                return 3
            # Приоритет 5: Обычный Frontend
            if "frontend" in title or "front-end" in title or "javascript" in title or "js" in title:
                return 4
            return 5

        # Сортируем и берем максимум 30 самых подходящих
        sorted_vacancies = sorted(unseen_vacancies, key=get_vacancy_priority)
        target_vacancies = sorted_vacancies[:30]

        # Загружаем историю для персонализации
        history = await get_recent_job_history(3, 3)
        
        if message:
            await message.edit_text(
                f"{status_text}🧠 <b>Оцениваю через ИИ {len(target_vacancies)} наиболее подходящих вакансий...</b>\n"
                f"(Всего отфильтровано новых: {len(unseen_vacancies)})",
                parse_mode="HTML"
            )

        # 3. Параллельный скоринг через ИИ
        sem = asyncio.Semaphore(4) # Ограничиваем количество одновременных запросов к Gemini
        passed_vacancies = []
        passed_lock = asyncio.Lock()

        async def score_vacancy(v):
            async with sem:
                try:
                    result = await process_job_scoring(v["title"], v["company"], v.get("description", ""), history=history)
                    
                    if _job_passes_filters(result):
                        full_verdict = _format_job_verdict(result, v.get("source", "Unknown"))
                        # Подготавливаем кортеж для пакетной записи
                        item = (
                            v["title"],
                            v["company"],
                            v["url"],
                            v["salary_raw"],
                            1 if v.get("is_remote", True) else 0,
                            result["score"],
                            full_verdict,
                            1 if result["has_salary"] else 0,
                            v.get("description", "")
                        )
                        async with passed_lock:
                            passed_vacancies.append(item)
                except Exception as e:
                    logger.error(f"Error scoring vacancy {v['url']}: {e}")

        # Запускаем скоринг для всех отобранных вакансий
        await asyncio.gather(*[score_vacancy(v) for v in target_vacancies])
        
        # 4. Пакетно сохраняем успешно прошедшие скоринг вакансии в БД
        new_vacancies_count = 0
        if passed_vacancies:
            if await save_vacancies_batch(passed_vacancies):
                new_vacancies_count = len(passed_vacancies)
                
        final_msg = (
            f"💼 <b>Поиск завершен!</b>\n\n"
            f"✅ Отобрано новых подходящих: <b>{new_vacancies_count}</b>\n"
            f"Проверено ИИ: {len(target_vacancies)} (всего найдено: {len(vacancies)})\n\n"
            f"Используйте команду /jobs или кнопку Список."
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

async def jobs_refresh_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной запуск поиска вакансий с прогресс-баром."""
    target_msg = update.effective_message
    status_msg = await target_msg.reply_text("🔎 Подготовка к поиску...")
    await job_fetch_job(context, message=status_msg)

async def list_jobs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выводит топ подходящих вакансий с пагинацией."""
    target_message = update.effective_message
    query = update.callback_query
    
    # Определяем текущий оффсет
    offset = 0
    if query and query.data.startswith("jobs_list_"):
        offset = int(query.data.split("_")[-1])
        await query.answer()

    limit = 5
    vacancies = await get_top_vacancies(limit=limit + 1, offset=offset) # Берем на 1 больше для проверки "далее"
    
    if not vacancies and offset == 0:
        await target_message.reply_text("💤 Пока новых подходящих вакансий не найдено.")
        return
    elif not vacancies:
        await query.answer("Это все доступные вакансии.")
        return

    has_more = len(vacancies) > limit
    display_vacancies = vacancies[:limit]

    for v in display_vacancies:
        vid = v[0]
        title = v[1]
        company = v[2]
        url = v[3]
        salary = v[4]
        score = v[6]
        verdict = v[7]
        
        match_emoji = "🟢" if score >= 8 else "🟡" if score >= 6 else "🔴"
        
        text = (
            f"{match_emoji} <b>{html.escape(title)}</b>\n"
            f"🏢 {html.escape(company)}\n"
            f"💰 {html.escape(salary)}\n"
            f"📊 Оценка: <b>{score}/10</b>\n\n"
            f"📝 <i>{html.escape(verdict)}</i>\n\n"
            f"🔗 {html.escape(url)}"
        )
        
        keyboard = [
            [InlineKeyboardButton("🔥 ГЕНЕРИРОВАТЬ ПИСЬМО", callback_data=f"cover_job_{vid}")],
            [InlineKeyboardButton("✅ Я откликнулся", callback_data=f"apply_job_{vid}"),
             InlineKeyboardButton("❌ Пропустить", callback_data=f"dismiss_job_{vid}")]
        ]
        
        try:
            if query and v == display_vacancies[0] and offset > 0:
                # Если это не первая страница, просто шлем новые сообщения
                await context.bot.send_message(update.effective_chat.id, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await target_message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            logger.error(f"Error sending job message: {e}")

    # Кнопки пагинации
    nav_buttons = []
    if offset > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"jobs_list_{max(0, offset-limit)}"))
    if has_more:
        nav_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"jobs_list_{offset+limit}"))
    
    if nav_buttons:
        await target_message.reply_text(f"Навигация (показано {offset+1}-{offset+len(display_vacancies)}):", 
                                     reply_markup=InlineKeyboardMarkup([nav_buttons]))

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

    new_query = " ".join(context.args)
    await set_trade_state("job_search_query", new_query)
    await target_msg.reply_text(
        f"✅ <b>Запрос изменен!</b>\nТеперь бот ищет: <code>{html.escape(new_query)}</code>",
        parse_mode="HTML"
    )

async def job_discovery_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поиск и добавление новых компаний через Gemini."""
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
    status_msg = await target_msg.reply_text("🤖 Gemini исследует рынок и ищет прямые ссылки на вакансии...")
    
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
