import logging
import html
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from database import get_watches, remove_watch, add_watch
from states import SELECT_SERVICE, INPUT_URL, INPUT_PRICE
import price_monitor

logger = logging.getLogger(__name__)

async def list_watches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    watches = await get_watches()
    if not watches:
        await update.message.reply_text("Список пуст")
        return
    
    await update.message.reply_text("📋 <b>Ваш список отслеживания:</b>", parse_mode="HTML")
    for w in watches:
        wid, _, name, _, target, last, service = w
        text = (f"📦 <b>{html.escape(name)}</b>\n"
                f"Служба: {html.escape(service or 'N/A')}\n"
                f"💰 Текущая: <code>{last}</code> -> 🎯 Цель: <code>{target}</code>")
        keyboard = [[InlineKeyboardButton("⚙️ Управление", callback_data=f"manage_{wid}")]]
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /unwatch <id>")
        return
    await remove_watch(int(context.args[0]))
    await update.message.reply_text("✅ Удалено из отслеживания.")

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
            await update.message.reply_text(f"✅ Мониторинг запущен: <b>{html.escape(name)}</b>", parse_mode="HTML")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        return ConversationHandler.END

async def show_news_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("🔍 Поиск новостей", callback_data="cmd_fetch")],
        [InlineKeyboardButton("➕ RSS", callback_data="add_rss_ui"), 
         InlineKeyboardButton("🗑 RSS", callback_data="del_rss_ui")],
        [InlineKeyboardButton("🔑 Слова", callback_data="add_kw_ui"),
         InlineKeyboardButton("🗑 Слова", callback_data="del_kw_ui")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")]
    ]
    await query.message.edit_text("🔍 <b>Управление новостями:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def add_rss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /addrss <url>")
        return
    url = context.args[0]
    from database import add_rss_feed
    if await add_rss_feed(url):
        await update.message.reply_text(f"✅ RSS добавлена: {url}")
    else:
        await update.message.reply_text("❌ Не удалось добавить.")

async def del_rss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /delrss <url>")
        return
    from database import remove_rss_feed
    if await remove_rss_feed(context.args[0]):
        await update.message.reply_text("✅ Удалено.")
    else:
        await update.message.reply_text("❌ Ошибка.")

async def list_rss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import get_rss_feeds
    feeds = await get_rss_feeds()
    text = "📰 <b>RSS-ленты:</b>\n\n" + ("\n".join([f"• {f}" for f in feeds]) if feeds else "Пусто")
    await update.message.reply_text(text, parse_mode="HTML")

async def add_kw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /addkw <word>")
        return
    from database import add_keyword
    if await add_keyword(context.args[0]):
        await update.message.reply_text(f"✅ Добавлено: {context.args[0]}")
    else:
        await update.message.reply_text("❌ Ошибка.")

async def del_kw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /delkw <word>")
        return
    from database import remove_keyword
    if await remove_keyword(context.args[0]):
        await update.message.reply_text("✅ Удалено.")
    else:
        await update.message.reply_text("❌ Ошибка.")

async def list_kw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import get_gaming_keywords
    kws = await get_gaming_keywords()
    text = "🔑 <b>Ключевые слова:</b>\n\n" + (", ".join(kws) if kws else "Пусто")
    await update.message.reply_text(text, parse_mode="HTML")
