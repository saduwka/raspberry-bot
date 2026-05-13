import os
import subprocess
import logging
import asyncio
import re
import html
import aiosqlite
from zoneinfo import ZoneInfo
from datetime import time as dt_time
from functools import wraps
from logging.handlers import RotatingFileHandler

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, ContextTypes, filters, ConversationHandler
)

import price_monitor
from config import BOT_TOKEN, ADMIN_ID, DB_PATH, RSS_FEEDS, GAMING_KEYWORDS, TRADE_INTERVAL_SECONDS
from database import (
    init_db, add_watch, get_watches, remove_watch, 
    add_blocked_tag, remove_blocked_tag, get_blocked_tags,
    get_pending, delete_pending, mark_posted, log_event,
    add_rss_feed, remove_rss_feed, get_rss_feeds,
    add_keyword, remove_keyword, get_gaming_keywords,
    save_pending, cleanup_old_data, update_target_price, get_watch_info,
    get_trade_state, set_trade_state, set_oled_config, populate_initial_data
)
from ai_utils import clean_html
from handlers.monitoring_handlers import (
    list_watches, unwatch, start_monitoring_interactive, 
    service_choice, process_url_step, process_price_step,
    show_news_menu, add_rss, del_rss, list_rss, add_kw, del_kw, list_kw
)
from handlers.system_handlers import oled_menu_handler, oled_callback_handler, show_settings, settings_callback
from jobs import (
    price_check_job, send_price_digest, check_health_alert,
    fetch_news, process_and_filter_news, send_for_approval,
    fetch_job, post_to_channel, send_weekly_digest, get_stats,
    trade_job, check_job_follow_ups, restart_bot, send_daily_trade_analytics
)
import trade_handlers
import job_handlers
from job_handlers import (
    job_fetch_job, list_jobs_handler, dismiss_job_callback,
    job_query_handler, jobs_refresh_handler, list_applied_jobs_handler,
    cover_letter_callback, job_discovery_handler
)

# --- Logging ---
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log_file = "bot.log"
file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3)
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler]
)
logger = logging.getLogger(__name__)

ALMATY_TZ = ZoneInfo("Asia/Almaty")

from states import (
    SELECT_SERVICE, INPUT_URL, INPUT_PRICE, EDIT_PRICE, ADD_RSS, 
    ADD_KW, JOB_QUERY_INPUT, JOB_DISCOVERY_INPUT, INPUT_SCROLL_TEXT
)
from handlers.monitoring_handlers import (
    list_watches, unwatch, start_monitoring_interactive, 
    service_choice, process_url_step, process_price_step,
    show_news_menu, add_rss, del_rss, list_rss, add_kw, del_kw, list_kw
)
from handlers.system_handlers import oled_menu_handler, oled_callback_handler, show_settings, settings_callback

# --- Decorators ---
def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id != ADMIN_ID:
            logger.warning(f"Unauthorized access attempt by ID: {user_id}")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- UI Helpers ---
def reply_keyboard():
    return ReplyKeyboardMarkup([
        ["💼 Вакансии", "📈 Трейдинг"],
        ["🛍 Мониторинг", "🔍 Новости"],
        ["⚙️ Система"]
    ], resize_keyboard=True)

def monitoring_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Мои товары", callback_data="cmd_watches")],
        [InlineKeyboardButton("➕ Добавить Kaspi", callback_data="step_kaspi"),
         InlineKeyboardButton("➕ Добавить OLX", callback_data="step_olx")],
    ])

def system_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статус RPi", callback_data="cmd_status")],
        [InlineKeyboardButton("📺 OLED Дисплей", callback_data="oled_menu")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="set_back"),
         InlineKeyboardButton("🔄 Перезапуск", callback_data="set_restart")],
    ])

# OLED handlers moved to handlers.system_handlers

async def process_scroll_text_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    await set_oled_config('scrolling_text', text)
    await update.message.reply_text("✅ Текст бегущей строки обновлен!", reply_markup=reply_keyboard())
    return ConversationHandler.END

def news_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Искать новости сейчас", callback_data="cmd_fetch")],
        [InlineKeyboardButton("📰 Настроить RSS", callback_data="set_rss"),
         InlineKeyboardButton("🔑 Ключевые слова", callback_data="set_kw")],
    ])

@admin_only
async def show_monitoring_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "🛍 <b>Управление мониторингом цен</b>\n\nПросматривайте активные товары или добавляйте новые ссылки с Kaspi и OLX."
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=monitoring_keyboard())
        except:
            await update.callback_query.message.delete()
            await context.bot.send_message(update.effective_chat.id, text, parse_mode="HTML", reply_markup=monitoring_keyboard())
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=monitoring_keyboard())

@admin_only
async def show_system_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "⚙️ <b>Системное меню</b>\n\nПроверка состояния оборудования и управление основными настройками бота."
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=system_keyboard())
        except:
            await update.callback_query.message.delete()
            await context.bot.send_message(update.effective_chat.id, text, parse_mode="HTML", reply_markup=system_keyboard())
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=system_keyboard())

@admin_only
async def show_news_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "🔍 <b>Игровые новости</b>\n\nРучной запуск парсера или настройка источников и фильтров."
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=news_keyboard())
        except:
            await update.callback_query.message.delete()
            await context.bot.send_message(update.effective_chat.id, text, parse_mode="HTML", reply_markup=news_keyboard())
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=news_keyboard())

def jobs_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Список", callback_data="jobs_list"),
         InlineKeyboardButton("🔄 Обновить", callback_data="jobs_refresh")],
        [InlineKeyboardButton("📝 Мои отклики", callback_data="jobs_applied"),
         InlineKeyboardButton("🔎 Текущий запрос", callback_data="jobs_query_show")],
        [InlineKeyboardButton("✏️ Изменить запрос", callback_data="jobs_query_edit"),
         InlineKeyboardButton("🕵️ Найти компании", callback_data="jobs_discovery_edit")],
    ])

@admin_only
async def show_jobs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "💼 <b>Вакансии</b>\n\n"
        "Управление поиском вакансий через кнопки."
    )
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=jobs_keyboard())
        except:
            await update.callback_query.message.delete()
            await context.bot.send_message(update.effective_chat.id, text, parse_mode="HTML", reply_markup=jobs_keyboard())
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=jobs_keyboard())

@admin_only
async def show_trade_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await trade_handlers.show_trade_menu(update, context)

# --- Command Handlers ---
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎮 Привет! Управление ботом:", reply_markup=reply_keyboard())

@admin_only
async def watch_kaspi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Использование: /watch_kaspi <url> <целевая_цена>")
        return
    url = context.args[0]
    try:
        target_price = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Цена должна быть числом")
        return
    
    await update.message.reply_text("⏳ Проверяю...")
    is_category = "/shop/c/" in url
    price, _, _ = await price_monitor.check_price(url)
    
    if price is None:
        await update.message.reply_text("❌ Не удалось получить цену. Проверьте URL.")
        return
    
    name = url.split("/")[-1].replace("-", " ").capitalize()
    if is_category: name = f"Категория: {name}"
    
    if await add_watch(update.effective_user.id, name, url, target_price, price, "Kaspi"):
        if is_category:
            items_to_silence = await price_monitor.get_kaspi_category_items(url)
            for item in items_to_silence:
                await mark_posted(item['url'])
        
        await update.message.reply_text(
            f"✅ <b>Добавлено: {html.escape(name)}</b>\n\n"
            f"🎯 Цель: <code>{target_price}</code>\n"
            "Все текущие товары в категории помечены как «прочитанные».",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("❌ Этот товар или категория уже отслеживается")

@admin_only
async def watch_olx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Использование: /watch_olx <url> <целевая_цена>")
        return
    url = context.args[0]
    try:
        target_price = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Цена должна быть числом")
        return
    
    await update.message.reply_text("⏳ Проверяю...")
    is_search = "search" in url or "rss" in url or "/q-" in url or not url.endswith(".html")
    price, _, items = await price_monitor.check_price(url)
    
    if price is None:
        await update.message.reply_text("❌ Не удалось получить цену. Проверьте URL.")
        return
    
    clean_url = url.split("?")[0].rstrip("/")
    slug = clean_url.split("/")[-1].replace(".html", "").replace("-", " ")
    
    if is_search:
        query_match = re.search(r'q-([^/?&]+)', url) or re.search(r'q=([^&]+)', url)
        if query_match:
            name = f"Поиск: {query_match.group(1).replace('+', ' ')}"
        else:
            cat_name = clean_url.split("/")[-2] if "list" in clean_url else slug
            name = f"Кат: {cat_name.capitalize()}"
    else:
        name = slug.capitalize()
        if not name or len(name) < 3:
            name = f"Товар OLX"
    
    if await add_watch(update.effective_user.id, name, url, target_price, price, "OLX"):
        if is_search:
            from price_monitor import get_olx_category_items
            items_to_silence = await get_olx_category_items(url)
            for item in items_to_silence:
                await mark_posted(item['url'])
        
        await update.message.reply_text(
            f"✅ <b>Добавлено: {html.escape(name)}</b>\n\n"
            f"🎯 Цель: <code>{target_price}</code>\n"
            "Я буду присылать уведомления только о <b>новых</b> объявлениях.",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("❌ Этот URL уже отслеживается")

@admin_only
async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /unwatch <id>")
        return
    await remove_watch(int(context.args[0]))
    await update.message.reply_text("✅ Удалено из отслеживания.")

# News and Keyword handlers moved to handlers.monitoring_handlers

# Settings handlers moved to handlers.system_handlers

# Handlers have been moved to handlers/monitoring_handlers.py

async def add_rss_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if url.startswith("/"): return ConversationHandler.END
    if "http" not in url:
        await update.message.reply_text("❌ Ошибка. Это не ссылка. Попробуйте еще раз или /cancel.")
        return ADD_RSS
    if await add_rss_feed(url):
        await update.message.reply_text(f"✅ RSS добавлена: {url}", reply_markup=reply_keyboard())
    else:
        await update.message.reply_text("❌ Не удалось добавить (возможно, уже есть).", reply_markup=reply_keyboard())
    return ConversationHandler.END

async def add_kw_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kw = update.message.text.lower()
    if kw.startswith("/"): return ConversationHandler.END
    if await add_keyword(kw):
        await update.message.reply_text(f"✅ Слово добавлено: {kw}", reply_markup=reply_keyboard())
    else:
        await update.message.reply_text("❌ Ошибка.", reply_markup=reply_keyboard())
    return ConversationHandler.END

# Monitoring interactive handlers moved to handlers.monitoring_handlers
# --- Message and Callback Handlers ---
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return
    data = query.data

    # Обработка настроек
    if data.startswith(("set_", "add_", "del_", "drss_", "dkw_", "confirm_")):
        return await settings_callback(update, context)
    elif data == "oled_scroll_set":
        await query.message.reply_text("Введите текст для бегущей строки (или /cancel):")
        return INPUT_SCROLL_TEXT

    # Обработка вакансий
    if data == "jobs_list":
        await list_jobs_handler(update, context)
    elif data == "jobs_refresh":
        await jobs_refresh_handler(update, context)
    elif data == "jobs_applied":
        await list_applied_jobs_handler(update, context)
    elif data == "jobs_query_show":
        await job_query_handler(update, context)
    elif data == "jobs_query_edit":
        await query.message.reply_text("Введите новый поисковый запрос (например: React Senior):")
        return JOB_QUERY_INPUT
    elif data == "jobs_discovery_edit":
        await query.message.reply_text("Опишите, какие компании искать (например: Финтех компании СНГ похожие на Каспи):")
        return JOB_DISCOVERY_INPUT

    elif data == "ipod_sync_push":
        await query.answer("🚀 Запускаю перенос...")
        # Run the sync script in the background
        cmd = ["python3", "/root/music_sync/sync.py", "--push-only"]
        try:
            # We use a non-blocking subprocess call
            subprocess.Popen(cmd)
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка запуска: {e}")

    if data == "cmd_status":
        await query.message.reply_text(get_stats(), parse_mode="HTML")
    elif data == "trade_menu":
        await show_trade_menu(update, context)
    elif data.startswith("trade_select_"):
        pair = data.replace("trade_select_", "")
        await trade_handlers.trade_stats_handler(update, context, pair=pair)
    elif data.startswith("trade_stats_"):
        pair = data.replace("trade_stats_", "")
        await trade_handlers.trade_stats_handler(update, context, pair=pair)
    elif data.startswith("trade_refresh_"):
        pair = data.replace("trade_refresh_", "")
        await query.answer(f"📊 Обновляю {pair}...")
        await trade_handlers.trade_stats_handler(update, context, pair=pair)
    elif data.startswith("trade_signal_"):
        pair = data.replace("trade_signal_", "")
        await query.answer(f"🧠 Анализирую {pair}...")
        await trade_handlers.trade_signal_handler(update, context, pair=pair)
    elif data == "cmd_fetch":
        await query.message.reply_text("🔍 Ищу новости...")
        news = await fetch_news()
        count = 0
        # Увеличиваем лимиты для ручного поиска (до 15 новостей, макс 8 на проверку)
        for item in news[:15]:
            processed_text = await process_and_filter_news(context.bot, item)
            if not processed_text: continue
            pid = await save_pending(item["title"], item["url"], processed_text, item.get("image_url"))
            await send_for_approval(context.bot, item, processed_text, pid)
            count += 1
            await asyncio.sleep(1)
            if count >= 8: break
        if count == 0: await query.message.reply_text("😴 Ничего нового")
    elif data == "cmd_watches":
        await list_watches(update, context)
    elif data.startswith("approve_"):
        pid = int(data.split("_")[1])
        await post_to_channel(context.bot, pid)
        await query.message.edit_text("✅ Опубликовано")
    elif data.startswith("reject_"):
        pid = int(data.split("_")[1])
        item = await get_pending(pid)
        if item: await mark_posted(item[2])
        await delete_pending(pid)
        await query.message.edit_text("❌ Отклонено")
    elif data.startswith("manage_"):
        wid = int(data.split("_")[1])
        row = await get_watch_info(wid)
        if row:
            text = f"⚙️ <b>{html.escape(row[0])}</b>\nЦель: <code>{row[1]}</code>"
            kb = [[InlineKeyboardButton("💰 Изменить цену", callback_data=f"editpr_{wid}"),
                   InlineKeyboardButton("🗑 Удалить", callback_data=f"unwatch_{wid}")]]
            await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("unwatch_"):
        wid = int(data.split("_")[1])
        await remove_watch(wid)
        await query.message.edit_text("🗑 Удалено")
    elif data.startswith("editpr_"):
        wid = int(data.split("_")[1])
        context.user_data["edit_wid"] = wid
        await query.message.reply_text("Введите новую целевую цену:")
        return EDIT_PRICE

async def process_edit_price_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        new_price = int(re.sub(r'[^\d]', '', update.message.text))
        wid = context.user_data.get("edit_wid")
        await update_target_price(wid, new_price)
        await update.message.reply_text(f"✅ Изменено на {new_price}", reply_markup=reply_keyboard())
        return ConversationHandler.END
    except:
        return EDIT_PRICE

async def process_job_query_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END

    new_query = update.message.text.strip()
    if not new_query or new_query.startswith("/"):
        await update.message.reply_text("❌ Изменение отменено.", reply_markup=reply_keyboard())
        return ConversationHandler.END

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
    
    # Мы можем просто вызвать существующий хендлер, передав текст сообщения в context.args
    context.args = update.message.text.strip().split()
    await job_discovery_handler(update, context)
    return ConversationHandler.END

@admin_only
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "🔍 Новости":
        await show_news_menu(update, context)
    elif text == "⚙️ Система":
        await show_system_menu(update, context)
    elif text == "📈 Трейдинг":
        await show_trade_menu(update, context)
    elif text == "🛍 Мониторинг":
        await show_monitoring_menu(update, context)
    elif text == "💼 Вакансии":
        await show_jobs_menu(update, context)
    elif text == "/start":
        await start(update, context)

async def on_startup(app):
    print("DEBUG: on_startup START")
    await populate_initial_data(RSS_FEEDS, GAMING_KEYWORDS)
    print("DEBUG: on_startup DATABASE INIT DONE")

    try:
        await app.bot.send_message(chat_id=ADMIN_ID, text=f"🚀 <b>Бот запущен!</b>\n\n{get_stats()}", parse_mode="HTML", reply_markup=reply_keyboard())
    except Exception as e:
        print(f"DEBUG: Startup message failed: {e}")
    print("DEBUG: on_startup END")

async def cancel_interactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено.", reply_markup=reply_keyboard())
    return ConversationHandler.END

def main():
    print("--- BOT STARTING VERSION 3.0 ---")
    asyncio.run(init_db())
    
    app = Application.builder().token(BOT_TOKEN).build()
    app.post_init = on_startup

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(service_choice, pattern="^step_"),
            CallbackQueryHandler(callback_handler, pattern="^(editpr_|add_rss_ui|add_kw_ui|jobs_query_edit|jobs_discovery_edit|oled_scroll_set)")
        ],
        states={
            SELECT_SERVICE: [CallbackQueryHandler(service_choice, pattern="^step_")],
            INPUT_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_url_step)],
            INPUT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_price_step)],
            EDIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_edit_price_step)],
            ADD_RSS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_rss_step)],
            ADD_KW: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_kw_step)],
            JOB_QUERY_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_job_query_step)],
            JOB_DISCOVERY_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_job_discovery_step)],
            INPUT_SCROLL_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_scroll_text_step)],
        },
        fallbacks=[CommandHandler("cancel", cancel_interactive), CallbackQueryHandler(service_choice, pattern="^step_cancel$")],
    )
    
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("watch_kaspi", watch_kaspi))
    app.add_handler(CommandHandler("watch_olx", watch_olx))
    app.add_handler(CommandHandler("watches", list_watches))
    app.add_handler(CommandHandler("unwatch", unwatch))
    app.add_handler(CommandHandler("restart", restart_bot))
    app.add_handler(CommandHandler("test_analytics", send_daily_trade_analytics))
    
    # OLED Handlers
    app.add_handler(CallbackQueryHandler(oled_menu_handler, pattern="^oled_menu$"))
    app.add_handler(CallbackQueryHandler(oled_callback_handler, pattern="^oled_(pwr|scr|restart)"))
    
    # Команды управления источниками (остаются как запасной вариант)
    app.add_handler(CommandHandler("addrss", add_rss))
    app.add_handler(CommandHandler("delrss", del_rss))
    app.add_handler(CommandHandler("listrss", list_rss))
    app.add_handler(CommandHandler("addkw", add_kw))
    app.add_handler(CommandHandler("delkw", del_kw))
    app.add_handler(CommandHandler("listkw", list_kw))
    app.add_handler(CommandHandler("jobs", list_jobs_handler))
    app.add_handler(CommandHandler("jobs_refresh", jobs_refresh_handler))
    app.add_handler(CommandHandler("job_query", job_query_handler))
    app.add_handler(CommandHandler("discovery", job_discovery_handler))

    app.add_handler(CallbackQueryHandler(cover_letter_callback, pattern="^cover_job_"))
    app.add_handler(CallbackQueryHandler(dismiss_job_callback, pattern="^(dismiss_job_|apply_job_)"))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    jq = app.job_queue
    # jq.run_daily(fetch_job, time=dt_time(9, 0, tzinfo=ALMATY_TZ))
    # jq.run_daily(fetch_job, time=dt_time(15, 0, tzinfo=ALMATY_TZ))
    # jq.run_daily(fetch_job, time=dt_time(21, 0, tzinfo=ALMATY_TZ))
    jq.run_daily(job_fetch_job, time=dt_time(10, 0, tzinfo=ALMATY_TZ))
    jq.run_daily(job_fetch_job, time=dt_time(18, 0, tzinfo=ALMATY_TZ))
    jq.run_daily(check_job_follow_ups, time=dt_time(11, 0, tzinfo=ALMATY_TZ))
    jq.run_daily(lambda ctx: asyncio.create_task(cleanup_old_data()), time=dt_time(4, 0, tzinfo=ALMATY_TZ))
    jq.run_daily(send_weekly_digest, time=dt_time(20, 0, tzinfo=ALMATY_TZ), days=(6,))
    jq.run_daily(send_daily_trade_analytics, time=dt_time(23, 0, tzinfo=ALMATY_TZ))
    jq.run_repeating(price_check_job, interval=1200, first=60)
    jq.run_repeating(send_price_digest, interval=1800, first=90)
    jq.run_repeating(check_health_alert, interval=300, first=10)
    jq.run_repeating(trade_job, interval=TRADE_INTERVAL_SECONDS, first=30)
    
    logger.info("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()
