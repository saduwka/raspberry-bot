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
from config import BOT_TOKEN, ADMIN_ID, DB_PATH, RSS_FEEDS, GAMING_KEYWORDS
from database import (
    init_db, add_watch, get_watches, remove_watch, 
    add_blocked_tag, remove_blocked_tag, get_blocked_tags,
    get_pending, delete_pending, mark_posted, log_event,
    add_rss_feed, remove_rss_feed, get_rss_feeds,
    add_keyword, remove_keyword, get_gaming_keywords,
    save_pending, cleanup_old_data, update_target_price, get_watch_info
)
from ai_utils import clean_html
from jobs import (
    price_check_job, send_price_digest, check_health_alert,
    fetch_news, process_and_filter_news, send_for_approval,
    fetch_job, post_to_channel, send_weekly_digest, get_stats
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
SELECT_SERVICE, INPUT_URL, INPUT_PRICE, EDIT_PRICE, ADD_RSS, ADD_KW = range(6)

# --- Decorators ---
def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id != ADMIN_ID:
            logger.warning(f"Unauthorized access attempt by {user_id}")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- UI Helpers ---
def reply_keyboard():
    return ReplyKeyboardMarkup([
        ["🔍 Новости", "📊 Статус"],
        ["🛍 Мониторинг", "📋 Мои товары"],
        ["⚙️ Настройки"]
    ], resize_keyboard=True)

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
async def list_watches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    watches = await get_watches()
    if not watches:
        await update.message.reply_text("Список пуст", reply_markup=reply_keyboard())
        return
    
    await update.message.reply_text("📋 <b>Ваш список отслеживания:</b>", parse_mode="HTML")
    for w in watches:
        wid, _, name, _, target, last, service = w
        text = (f"📦 <b>{html.escape(name)}</b>\n"
                f"Служба: {html.escape(service or 'N/A')}\n"
                f"💰 Текущая: <code>{last}</code> -> 🎯 Цель: <code>{target}</code>")
        keyboard = [[InlineKeyboardButton("⚙️ Управление", callback_data=f"manage_{wid}")]]
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

@admin_only
async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /unwatch <id>")
        return
    await remove_watch(int(context.args[0]))
    await update.message.reply_text("✅ Удалено из отслеживания.")

# --- Sources Management ---
@admin_only
async def add_rss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /addrss <url>")
        return
    url = context.args[0]
    if await add_rss_feed(url):
        await update.message.reply_text(f"✅ RSS-лента добавлена: {url}")
    else:
        await update.message.reply_text("❌ Ошибка (возможно, уже есть)")

@admin_only
async def del_rss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /delrss <url>")
        return
    url = context.args[0]
    await remove_rss_feed(url)
    await update.message.reply_text(f"✅ RSS-лента удалена: {url}")

@admin_only
async def list_rss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    feeds = await get_rss_feeds()
    if not feeds:
        await update.message.reply_text("Список RSS-лент пуст.")
    else:
        await update.message.reply_text("📋 <b>Активные RSS-ленты:</b>\n\n" + "\n".join(feeds), parse_mode="HTML")

@admin_only
async def add_kw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /addkw <слово>")
        return
    kw = " ".join(context.args).lower()
    if await add_keyword(kw):
        await update.message.reply_text(f"✅ Ключевое слово добавлено: {kw}")
    else:
        await update.message.reply_text("❌ Ошибка")

@admin_only
async def del_kw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /delkw <слово>")
        return
    kw = " ".join(context.args).lower()
    await remove_keyword(kw)
    await update.message.reply_text(f"✅ Ключевое слово удалено: {kw}")

@admin_only
async def list_kw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kws = await get_gaming_keywords()
    if not kws:
        await update.message.reply_text("Список ключевых слов пуст.")
    else:
        text = "📋 <b>Ключевые слова:</b>\n\n" + ", ".join(kws)
        if len(text) > 4000:
            for i in range(0, len(text), 4000):
                await update.message.reply_text(text[i:i+4000], parse_mode="HTML")
        else:
            await update.message.reply_text(text, parse_mode="HTML")

# --- Interactive Settings ---
@admin_only
async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📰 Управление RSS", callback_data="set_rss"),
         InlineKeyboardButton("🔑 Ключевые слова", callback_data="set_kw")],
        [InlineKeyboardButton("🚫 Стоп-теги", callback_data="set_tags"),
         InlineKeyboardButton("🧹 Очистка БД", callback_data="set_cleanup")],
        [InlineKeyboardButton("❌ Закрыть", callback_data="set_close")]
    ]
    await update.message.reply_text("⚙️ <b>Настройки бота</b>\n\nВыберите раздел для управления:", 
                                   reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return
    data = query.data

    if data == "set_close":
        await query.message.delete()
    elif data == "set_rss":
        feeds = await get_rss_feeds()
        text = "📰 <b>RSS-ленты:</b>\n\n" + ("\n".join([f"• {f}" for f in feeds]) if feeds else "Пусто")
        kb = [[InlineKeyboardButton("➕ Добавить ленту", callback_data="add_rss_ui")],
              [InlineKeyboardButton("🗑 Удалить ленту", callback_data="del_rss_ui")],
              [InlineKeyboardButton("⬅️ Назад", callback_data="set_back")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML", disable_web_page_preview=True)
    elif data == "set_kw":
        kws = await get_gaming_keywords()
        text = "🔑 <b>Ключевые слова:</b>\n\n" + (", ".join(kws) if kws else "Пусто")
        kb = [[InlineKeyboardButton("➕ Добавить слово", callback_data="add_kw_ui")],
              [InlineKeyboardButton("🗑 Удалить слово", callback_data="del_kw_ui")],
              [InlineKeyboardButton("⬅️ Назад", callback_data="set_back")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    elif data == "set_tags":
        tags = await get_blocked_tags()
        text = "🚫 <b>Заблокированные теги:</b>\n\n" + (", ".join(tags) if tags else "Пусто")
        kb = [[InlineKeyboardButton("⬅️ Назад", callback_data="set_back")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    elif data == "set_back":
        keyboard = [
            [InlineKeyboardButton("📰 Управление RSS", callback_data="set_rss"),
             InlineKeyboardButton("🔑 Ключевые слова", callback_data="set_kw")],
            [InlineKeyboardButton("🚫 Стоп-теги", callback_data="set_tags"),
             InlineKeyboardButton("🧹 Очистка БД", callback_data="set_cleanup")],
            [InlineKeyboardButton("❌ Закрыть", callback_data="set_close")]
        ]
        await query.message.edit_text("⚙️ <b>Настройки бота</b>\n\nВыберите раздел для управления:", 
                                     reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    elif data == "set_cleanup":
        await cleanup_old_data()
        await query.message.edit_text("✅ База данных очищена (удалены старые логи и новости).", 
                                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="set_back")]]))
    elif data == "add_rss_ui":
        await query.message.reply_text("Отправьте URL новой RSS-ленты (или /cancel):")
        return ADD_RSS
    elif data == "add_kw_ui":
        await query.message.reply_text("Введите новое ключевое слово (или /cancel):")
        return ADD_KW
    elif data == "del_rss_ui":
        feeds = await get_rss_feeds()
        if not feeds:
            await query.answer("Список пуст")
            return
        kb = [[InlineKeyboardButton(f"🗑 {f[:30]}...", callback_data=f"drss_{i}")] for i, f in enumerate(feeds)]
        kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="set_rss")])
        await query.message.edit_text("Выберите ленту для удаления:", reply_markup=InlineKeyboardMarkup(kb))
    elif data == "del_kw_ui":
        kws = await get_gaming_keywords()
        if not kws:
            await query.answer("Список пуст")
            return
        kb = [[InlineKeyboardButton(f"🗑 {k}", callback_data=f"dkw_{i}")] for i, k in enumerate(kws)]
        kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="set_kw")])
        await query.message.edit_text("Выберите слово для удаления:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("drss_"):
        idx = int(data.split("_")[1])
        feeds = await get_rss_feeds()
        if idx < len(feeds):
            await remove_rss_feed(feeds[idx])
            await query.answer(f"Удалено: {feeds[idx][:20]}...")
            # Показываем обновленный список
            feeds = await get_rss_feeds()
            text = "📰 <b>RSS-ленты:</b>\n\n" + ("\n".join([f"• {f}" for f in feeds]) if feeds else "Пусто")
            kb = [[InlineKeyboardButton("➕ Добавить ленту", callback_data="add_rss_ui")],
                  [InlineKeyboardButton("🗑 Удалить ленту", callback_data="del_rss_ui")],
                  [InlineKeyboardButton("⬅️ Назад", callback_data="set_back")]]
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML", disable_web_page_preview=True)
    elif data.startswith("dkw_"):
        idx = int(data.split("_")[1])
        kws = await get_gaming_keywords()
        if idx < len(kws):
            await remove_keyword(kws[idx])
            await query.answer(f"Удалено: {kws[idx]}")
            # Показываем обновленный список
            kws = await get_gaming_keywords()
            text = "🔑 <b>Ключевые слова:</b>\n\n" + (", ".join(kws) if kws else "Пусто")
            kb = [[InlineKeyboardButton("➕ Добавить слово", callback_data="add_kw_ui")],
                  [InlineKeyboardButton("🗑 Удалить слово", callback_data="del_kw_ui")],
                  [InlineKeyboardButton("⬅️ Назад", callback_data="set_back")]]
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

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

# --- Interactive Monitoring Setup ---
@admin_only
async def start_monitoring_interactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Kaspi 🇰🇿", callback_data="step_kaspi"),
         InlineKeyboardButton("OLX 🟦", callback_data="step_olx")],
        [InlineKeyboardButton("❌ Отмена", callback_data="step_cancel")]
    ]
    await update.message.reply_text(
        "🚀 *Настройка мониторинга*\n\nВыберите сервис, который хотите отслеживать:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return SELECT_SERVICE

async def service_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return
    
    if query.data == "step_cancel":
        await query.message.edit_text("❌ Настройка отменена.")
        return ConversationHandler.END
    service = "Kaspi" if query.data == "step_kaspi" else "OLX"
    context.user_data["tmp_service"] = service
    await query.message.edit_text(f"✅ Выбран сервис: *{service}*\n\nТеперь *отправьте ссылку*:", parse_mode="Markdown")
    return INPUT_URL

async def process_url_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if "http" not in url:
        await update.message.reply_text("❌ Это не похоже на ссылку. Попробуйте еще раз.")
        return INPUT_URL
    context.user_data["tmp_url"] = url
    await update.message.reply_text("📊 *Целевая цена* (числом):", parse_mode="Markdown")
    return INPUT_PRICE

async def process_price_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_price = int(re.sub(r'[^\d]', '', update.message.text))
        service = context.user_data.get("tmp_service")
        url = context.user_data.get("tmp_url")
        user_id = update.effective_user.id
        await update.message.reply_text("⏳ Проверяю...")
        
        price, _, items = await price_monitor.check_price(url)
        if price is None:
            await update.message.reply_text("❌ Ошибка. Проверьте ссылку.")
            return ConversationHandler.END

        name = url.split("/")[-1].replace("-", " ").replace(".html", "").capitalize()
        if await add_watch(user_id, name, url, target_price, price, service):
            if items:
                for item in items: await mark_posted(item['url'])
            await update.message.reply_text(f"✅ Мониторинг запущен: <b>{html.escape(name)}</b>", parse_mode="HTML", reply_markup=reply_keyboard())
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return ConversationHandler.END

# --- Message and Callback Handlers ---
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return
    data = query.data

    # Обработка настроек
    if data.startswith(("set_", "add_", "del_", "drss_", "dkw_")):
        return await settings_callback(update, context)

    if data == "cmd_status":
        await query.message.reply_text(get_stats(), parse_mode="HTML")
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

@admin_only
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "🔍 Новости":
        await fetch_job(context)
    elif text == "📊 Статус":
        await update.message.reply_text(get_stats(), parse_mode="HTML")
    elif text == "🛍 Мониторинг":
        return await start_monitoring_interactive(update, context)
    elif text == "📋 Мои товары":
        await list_watches(update, context)
    elif text == "⚙️ Настройки":
        await show_settings(update, context)

async def on_startup(app):
    # Первоначальная инициализация базы из конфига, если пустая
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        async with db.execute("SELECT COUNT(*) FROM rss_feeds") as cursor:
            row = await cursor.fetchone()
            if row[0] == 0:
                logger.info("Populating initial RSS feeds...")
                for url in RSS_FEEDS:
                    await db.execute("INSERT OR IGNORE INTO rss_feeds (url) VALUES (?)", (url,))
        
        async with db.execute("SELECT COUNT(*) FROM gaming_keywords") as cursor:
            row = await cursor.fetchone()
            if row[0] == 0:
                logger.info("Populating initial keywords...")
                for kw in GAMING_KEYWORDS:
                    await db.execute("INSERT OR IGNORE INTO gaming_keywords (keyword) VALUES (?)", (kw.lower(),))
        await db.commit()

    await app.bot.send_message(chat_id=ADMIN_ID, text=f"🚀 <b>Бот запущен!</b>\n\n{get_stats()}", parse_mode="HTML", reply_markup=reply_keyboard())

async def cancel_interactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено.", reply_markup=reply_keyboard())
    return ConversationHandler.END

def main():
    asyncio.run(init_db())
    
    app = Application.builder().token(BOT_TOKEN).build()
    app.post_init = on_startup

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🛍 Мониторинг$"), start_monitoring_interactive),
            CallbackQueryHandler(callback_handler, pattern="^(editpr_|add_rss_ui|add_kw_ui)")
        ],
        states={
            SELECT_SERVICE: [CallbackQueryHandler(service_choice, pattern="^step_")],
            INPUT_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_url_step)],
            INPUT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_price_step)],
            EDIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_edit_price_step)],
            ADD_RSS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_rss_step)],
            ADD_KW: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_kw_step)],
        },
        fallbacks=[CommandHandler("cancel", cancel_interactive), CallbackQueryHandler(service_choice, pattern="^step_cancel$")],
    )
    
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("watch_kaspi", watch_kaspi))
    app.add_handler(CommandHandler("watch_olx", watch_olx))
    app.add_handler(CommandHandler("watches", list_watches))
    app.add_handler(CommandHandler("unwatch", unwatch))
    
    # Команды управления источниками (остаются как запасной вариант)
    app.add_handler(CommandHandler("addrss", add_rss))
    app.add_handler(CommandHandler("delrss", del_rss))
    app.add_handler(CommandHandler("listrss", list_rss))
    app.add_handler(CommandHandler("addkw", add_kw))
    app.add_handler(CommandHandler("delkw", del_kw))
    app.add_handler(CommandHandler("listkw", list_kw))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    jq = app.job_queue
    jq.run_daily(fetch_job, time=dt_time(9, 0, tzinfo=ALMATY_TZ))
    jq.run_daily(fetch_job, time=dt_time(15, 0, tzinfo=ALMATY_TZ))
    jq.run_daily(fetch_job, time=dt_time(21, 0, tzinfo=ALMATY_TZ))
    jq.run_daily(lambda ctx: asyncio.create_task(cleanup_old_data()), time=dt_time(4, 0, tzinfo=ALMATY_TZ))
    jq.run_daily(send_weekly_digest, time=dt_time(20, 0, tzinfo=ALMATY_TZ), days=(6,))
    jq.run_repeating(price_check_job, interval=1200, first=60)
    jq.run_repeating(send_price_digest, interval=1800, first=90)
    jq.run_repeating(check_health_alert, interval=300, first=10)
    
    logger.info("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()
