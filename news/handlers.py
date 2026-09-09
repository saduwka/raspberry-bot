import asyncio
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CommandHandler, ContextTypes, ConversationHandler

from config import ADMIN_ID
from core.auth import admin_only
from core.states import ADD_KW, ADD_RSS
from core.ui import reply_keyboard
from news.fetcher import fetch_news
from news.jobs import post_to_channel, process_and_filter_news, send_for_approval
from news.repo import (
    add_keyword,
    add_rss_feed,
    delete_pending,
    get_gaming_keywords,
    get_pending,
    get_rss_feeds,
    mark_posted,
    remove_keyword,
    remove_rss_feed,
    save_pending,
)

logger = logging.getLogger(__name__)


def news_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Искать новости сейчас", callback_data="cmd_fetch")],
        [
            InlineKeyboardButton("📰 Настроить RSS", callback_data="set_rss"),
            InlineKeyboardButton("🔑 Ключевые слова", callback_data="set_kw"),
        ],
    ])


@admin_only
async def show_news_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "🔍 <b>Игровые новости</b>\n\nРучной запуск парсера или настройка источников и фильтров."
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=news_keyboard())
        except Exception:
            await update.callback_query.message.delete()
            await context.bot.send_message(
                update.effective_chat.id, text, parse_mode="HTML", reply_markup=news_keyboard()
            )
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=news_keyboard())


@admin_only
async def add_rss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /addrss <url>")
        return
    url = context.args[0]
    if await add_rss_feed(url):
        await update.message.reply_text(f"✅ RSS добавлена: {url}")
    else:
        await update.message.reply_text("❌ Не удалось добавить.")


@admin_only
async def del_rss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /delrss <url>")
        return
    if await remove_rss_feed(context.args[0]):
        await update.message.reply_text("✅ Удалено.")
    else:
        await update.message.reply_text("❌ Ошибка.")


@admin_only
async def list_rss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    feeds = await get_rss_feeds()
    text = "📰 <b>RSS-ленты:</b>\n\n" + ("\n".join([f"• {f}" for f in feeds]) if feeds else "Пусто")
    await update.message.reply_text(text, parse_mode="HTML")


@admin_only
async def add_kw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /addkw <word>")
        return
    if await add_keyword(context.args[0]):
        await update.message.reply_text(f"✅ Добавлено: {context.args[0]}")
    else:
        await update.message.reply_text("❌ Ошибка.")


@admin_only
async def del_kw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /delkw <word>")
        return
    if await remove_keyword(context.args[0]):
        await update.message.reply_text("✅ Удалено.")
    else:
        await update.message.reply_text("❌ Ошибка.")


@admin_only
async def list_kw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kws = await get_gaming_keywords()
    text = "🔑 <b>Ключевые слова:</b>\n\n" + (", ".join(kws) if kws else "Пусто")
    await update.message.reply_text(text, parse_mode="HTML")


async def add_rss_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if url.startswith("/"):
        return ConversationHandler.END
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
    if kw.startswith("/"):
        return ConversationHandler.END
    if await add_keyword(kw):
        await update.message.reply_text(f"✅ Слово добавлено: {kw}", reply_markup=reply_keyboard())
    else:
        await update.message.reply_text("❌ Ошибка.", reply_markup=reply_keyboard())
    return ConversationHandler.END


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    query = update.callback_query
    if data == "cmd_fetch":
        await query.message.reply_text("🔍 Ищу новости...")
        news = await fetch_news()
        count = 0
        for item in news[:15]:
            processed_text = await process_and_filter_news(context.bot, item)
            if not processed_text:
                continue
            pid = await save_pending(item["title"], item["url"], processed_text, item.get("image_url"))
            await send_for_approval(context.bot, item, processed_text, pid)
            count += 1
            await asyncio.sleep(1)
            if count >= 8:
                break
        if count == 0:
            await query.message.reply_text("😴 Ничего нового")
        return True
    if data.startswith("approve_"):
        pid = int(data.split("_")[1])
        await post_to_channel(context.bot, pid)
        await query.message.edit_text("✅ Опубликовано")
        return True
    if data.startswith("reject_"):
        pid = int(data.split("_")[1])
        item = await get_pending(pid)
        if item:
            await mark_posted(item[2])
        await delete_pending(pid)
        await query.message.edit_text("❌ Отклонено")
        return True
    return None


def register(app):
    app.add_handler(CommandHandler("addrss", add_rss))
    app.add_handler(CommandHandler("delrss", del_rss))
    app.add_handler(CommandHandler("listrss", list_rss))
    app.add_handler(CommandHandler("addkw", add_kw))
    app.add_handler(CommandHandler("delkw", del_kw))
    app.add_handler(CommandHandler("listkw", list_kw))
