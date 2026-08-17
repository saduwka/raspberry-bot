import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CommandHandler, ContextTypes, ConversationHandler

from ai.jobs import generate_cover_letter, suggest_new_companies
from config import ADMIN_ID, JOB_MIN_SCORE, RESUME_URL
from core.auth import admin_only
from core.states import JOB_DISCOVERY_INPUT, JOB_QUERY_INPUT
from core.ui import reply_keyboard
from jobs.jobs import _normalize_search_query, job_fetch_job
from jobs.repo import (
    dismiss_vacancy,
    get_applied_vacancies,
    get_dismissed_vacancies,
    get_job_stats,
    get_top_vacancies,
    get_vacancy_details,
    mark_vacancy_applied,
)
from trade.repo import get_trade_state, set_trade_state

logger = logging.getLogger(__name__)


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


def jobs_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Список", callback_data="jobs_list"),
         InlineKeyboardButton("🔄 Обновить", callback_data="jobs_refresh")],
        [InlineKeyboardButton("📝 Мои отклики", callback_data="jobs_applied"),
         InlineKeyboardButton("📁 Архив", callback_data="jobs_archive_0")],
        [InlineKeyboardButton("📊 Статистика", callback_data="jobs_stats"),
         InlineKeyboardButton("🔎 Текущий запрос", callback_data="jobs_query_show")],
        [InlineKeyboardButton("✏️ Изменить запрос", callback_data="jobs_query_edit"),
         InlineKeyboardButton("🕵️ Найти компании", callback_data="jobs_discovery_edit")],
    ])


@admin_only
async def show_jobs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "💼 <b>Вакансии</b>\n\nУправление поиском вакансий через кнопки."
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=jobs_keyboard())
        except Exception:
            await update.callback_query.message.delete()
            await context.bot.send_message(
                update.effective_chat.id, text, parse_mode="HTML", reply_markup=jobs_keyboard()
            )
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=jobs_keyboard())


@admin_only
async def jobs_refresh_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_msg = update.effective_message
    status_msg = await target_msg.reply_text("🔎 Подготовка к поиску...")
    await job_fetch_job(context, message=status_msg)


@admin_only
async def list_jobs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            logger.error("Error sending job message: %s", e)

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
    query = update.callback_query
    await query.answer("✍️ Формирую пакет для отклика...")

    vid = int(query.data.split("_")[-1])
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
    target_message = update.effective_message
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
        await mark_vacancy_applied(vid)
        await query.message.edit_text("✅ Отлично! Я напомню написать им через 7 дней.")
    else:
        await dismiss_vacancy(vid)
        await query.message.edit_text("📁 Вакансия перенесена в архив.")


@admin_only
async def job_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_msg = update.effective_message
    if not context.args:
        current = await get_trade_state("job_search_query")
        current = current if current else "Vue TypeScript Frontend (по умолчанию)"
        await target_msg.reply_text(
            f"🔎 <b>Текущий поисковый запрос:</b>\n<code>{html.escape(current)}</code>\n\n"
            f"Чтобы изменить, напишите: <code>/job_query React Middle/Senior</code>",
            parse_mode="HTML",
        )
        return

    new_query = _normalize_search_query(" ".join(context.args))
    await set_trade_state("job_search_query", new_query)
    await target_msg.reply_text(
        f"✅ <b>Запрос изменен!</b>\nТеперь бот ищет: <code>{html.escape(new_query)}</code>",
        parse_mode="HTML",
    )


@admin_only
async def job_discovery_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_msg = update.effective_message
    if not context.args:
        await target_msg.reply_text(
            "🔎 <b>Discovery Mode</b>\n\n"
            "Напишите, какие компании искать. Например:\n"
            "<code>/discovery Финтех компании СНГ похожие на Каспи</code>\n"
            "<code>/discovery Стартапы в Европе с фокусом на AI и React</code>",
            parse_mode="HTML",
        )
        return

    prompt = " ".join(context.args)
    status_msg = await target_msg.reply_text("🤖 local/qwen3.5-coder ищет компании и прямые ссылки на вакансии...")

    added_count, companies = await suggest_new_companies(prompt)

    if added_count > 0:
        names = ", ".join([c["name"] for c in companies])
        text = (
            f"✅ <b>Найдено и добавлено компаний: {added_count}</b>\n\n"
            f"Список: <i>{html.escape(names)}</i>\n\n"
            f"Теперь при каждом поиске (раз в день или через /jobs_refresh) "
            f"я буду проверять их карьерные страницы."
        )
    else:
        text = "😔 К сожалению, не удалось найти новые подходящие компании или они уже есть в списке."

    await status_msg.edit_text(text, parse_mode="HTML")


async def process_job_query_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END

    new_query = update.message.text.strip()
    if not new_query or new_query.startswith("/"):
        await update.message.reply_text("❌ Изменение отменено.", reply_markup=reply_keyboard())
        return ConversationHandler.END

    new_query = new_query.strip().strip('"').strip("'")
    await set_trade_state("job_search_query", new_query)
    await update.message.reply_text(
        f"✅ <b>Запрос изменен!</b>\nТеперь бот ищет: <code>{html.escape(new_query)}</code>",
        parse_mode="HTML",
        reply_markup=reply_keyboard(),
    )
    await update.message.reply_text("💼 Меню вакансий:", parse_mode="HTML", reply_markup=jobs_keyboard())
    return ConversationHandler.END


async def process_job_discovery_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    context.args = update.message.text.strip().split()
    await job_discovery_handler(update, context)
    return ConversationHandler.END


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    query = update.callback_query
    if data == "jobs_list" or data.startswith("jobs_list_"):
        await list_jobs_handler(update, context)
        return True
    if data == "jobs_refresh":
        await jobs_refresh_handler(update, context)
        return True
    if data == "jobs_applied":
        await list_applied_jobs_handler(update, context)
        return True
    if data == "jobs_stats":
        await jobs_stats_handler(update, context)
        return True
    if data == "jobs_archive_0" or data.startswith("jobs_archive_"):
        await list_archive_jobs_handler(update, context)
        return True
    if data == "jobs_query_show":
        await job_query_handler(update, context)
        return True
    if data == "jobs_query_edit":
        await query.message.reply_text("Введите новый поисковый запрос (например: React Middle/Senior):")
        return JOB_QUERY_INPUT
    if data == "jobs_discovery_edit":
        await query.message.reply_text("Опишите, какие компании искать (например: Финтех компании СНГ похожие на Каспи):")
        return JOB_DISCOVERY_INPUT
    return None


def register(app):
    app.add_handler(CommandHandler("jobs", list_jobs_handler))
    app.add_handler(CommandHandler("jobs_refresh", jobs_refresh_handler))
    app.add_handler(CommandHandler("jobs_stats", jobs_stats_handler))
    app.add_handler(CommandHandler("job_query", job_query_handler))
    app.add_handler(CommandHandler("discovery", job_discovery_handler))
