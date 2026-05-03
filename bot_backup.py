import os
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
    get_trade_state, set_trade_state
)
from ai_utils import clean_html
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

# Состояния диалога
SELECT_SERVICE, INPUT_URL, INPUT_PRICE, EDIT_PRICE, ADD_RSS, ADD_KW, JOB_QUERY_INPUT, JOB_DISCOVERY_INPUT, INPUT_SCROLL_TEXT = range(9)

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

async def oled_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔋 Вкл", callback_data="oled_pwr_on"), 
         InlineKeyboardButton("🔌 Выкл", callback_data="oled_pwr_off")],
        [InlineKeyboardButton("🔄 Авто-цикл", callback_data="oled_scr_-1"),
         InlineKeyboardButton("⚡️ Рестарт Сервиса", callback_data="oled_restart")],
        [InlineKeyboardButton("1: Sys", callback_data="oled_scr_0"),
         InlineKeyboardButton("2: Wth", callback_data="oled_scr_1")],
        [InlineKeyboardButton("💬 Бегущая строка", callback_data="oled_scroll_set")],
        [InlineKeyboardButton("3: Bot", callback_data="oled_scr_2"),
         InlineKeyboardButton("4: Trade", callback_data="oled_scr_3")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_system")]
    ])
    
    await query.edit_message_text("📺 Управление OLED монитором:", reply_markup=keyboard)

async def oled_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data == "oled_restart":
        os.system("systemctl restart oled_monitor.service")
        msg = "⚡️ Сервис OLED перезапущен"
    elif data == "oled_scroll_set":
        await query.message.reply_text("Введите текст для бегущей строки (или /cancel):")
        return INPUT_SCROLL_TEXT
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            if data.startswith("oled_pwr_"):
                pwr = data.split("_")[2]
                await db.execute("INSERT OR REPLACE INTO oled_config (key, value) VALUES ('power', ?)", (pwr,))
            elif data.startswith("oled_scr_"):
                scr = data.split("_")[2]
                await db.execute("INSERT OR REPLACE INTO oled_config (key, value) VALUES ('forced_screen', ?)", (scr,))
            await db.commit()
        msg = f"✅ Команда OLED сохранена ({data})"
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="oled_menu")]]))
    return ConversationHandler.END

async def process_scroll_text_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO oled_config (key, value) VALUES ('scrolling_text', ?)", (text,))
        await db.commit()
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
    
