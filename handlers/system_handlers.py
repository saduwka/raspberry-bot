import os
import logging
import aiosqlite
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from config import DB_PATH

from states import INPUT_SCROLL_TEXT

logger = logging.getLogger(__name__)

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
    
    from telegram.ext import ConversationHandler
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="oled_menu")]]))
    return ConversationHandler.END

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    keyboard = [
        [InlineKeyboardButton("📰 Управление RSS", callback_data="set_rss"),
         InlineKeyboardButton("🔑 Ключевые слова", callback_data="set_kw")],
        [InlineKeyboardButton("🚫 Стоп-теги", callback_data="set_tags"),
         InlineKeyboardButton("🧹 Очистка БД", callback_data="set_cleanup")],
        [InlineKeyboardButton("🔄 ПЕРЕЗАПУСК", callback_data="set_restart"),
         InlineKeyboardButton("❌ Закрыть", callback_data="set_close")]
    ]
    await update.message.reply_text("⚙️ <b>Настройки бота</b>\n\nВыберите раздел для управления:",
                                   reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    from database import get_rss_feeds, get_gaming_keywords, get_blocked_tags, cleanup_old_data, remove_rss_feed, remove_keyword
    from config import ADMIN_ID
    from jobs import restart_bot
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return
    data = query.data

    if data == "set_close":
        await query.message.delete()
    elif data == "set_rss":
        feeds = await get_rss_feeds()
        text = "📰 <b>RSS-ленты:</b>\n\n" + ("\n".join([f"• {html.escape(f)}" for f in feeds]) if feeds else "Пусто")
        kb = [[InlineKeyboardButton("➕ Добавить ленту", callback_data="add_rss_ui")],
              [InlineKeyboardButton("🗑 Удалить ленту", callback_data="del_rss_ui")],
              [InlineKeyboardButton("⬅️ Назад", callback_data="set_back")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML", disable_web_page_preview=True)
    elif data == "set_kw":
        kws = await get_gaming_keywords()
        text = "🔑 <b>Ключевые слова:</b>\n\n" + (", ".join([html.escape(k) for k in kws]) if kws else "Пусто")
        kb = [[InlineKeyboardButton("➕ Добавить слово", callback_data="add_kw_ui")],
              [InlineKeyboardButton("🗑 Удалить слово", callback_data="del_kw_ui")],
              [InlineKeyboardButton("⬅️ Назад", callback_data="set_back")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    elif data == "set_tags":
        tags = await get_blocked_tags()
        text = "🚫 <b>Заблокированные теги:</b>\n\n" + (", ".join([html.escape(t) for t in tags]) if tags else "Пусто")
        kb = [[InlineKeyboardButton("⬅️ Назад", callback_data="set_back")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    elif data == "set_back":
        keyboard = [
            [InlineKeyboardButton("📰 Управление RSS", callback_data="set_rss"),
             InlineKeyboardButton("🔑 Ключевые слова", callback_data="set_kw")],
            [InlineKeyboardButton("🚫 Стоп-теги", callback_data="set_tags"),
             InlineKeyboardButton("🧹 Очистка БД", callback_data="set_cleanup")],
            [InlineKeyboardButton("🔄 ПЕРЕЗАПУСК", callback_data="set_restart"),
             InlineKeyboardButton("❌ Закрыть", callback_data="set_close")]
        ]
        await query.message.edit_text("⚙️ <b>Настройки бота</b>\n\nВыберите раздел для управления:",
                                     reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    elif data == "set_restart":
        kb = [[InlineKeyboardButton("✅ ДА, ПЕРЕЗАГРУЗИТЬ", callback_data="confirm_restart")],
              [InlineKeyboardButton("⬅️ НАЗАД", callback_data="set_back")]]
        await query.message.edit_text("⚠️ <b>Вы уверены, что хотите перезагрузить бота?</b>",
                                     reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    elif data == "confirm_restart":
        await query.message.delete()
        await restart_bot(context=context)
    elif data == "set_cleanup":
        await cleanup_old_data()
        await query.message.edit_text("✅ База данных очищена (удалены старые логи и новости).",
                                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="set_back")]]))
