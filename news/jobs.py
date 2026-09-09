import asyncio
import gc
import html
import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ai.news import summarize_news
from config import ADMIN_ID, CHANNEL_ID
from core.db import log_event
from news.fetcher import fetch_news
from news.repo import (
    get_blocked_tags,
    get_pending,
    get_weekly_stats,
    mark_posted,
    save_pending,
)
from system.health import get_uptime

logger = logging.getLogger(__name__)


async def process_and_filter_news(bot, item):
    result = await summarize_news(item["title"], item["summary"])
    res_summary = result.get("summary", "")
    res_tags = [t.lower() for t in result.get("tags", [])]
    sentiment = result.get("sentiment", 0)

    await log_event("news_sentiment", {"url": item["url"], "sentiment": sentiment})

    blocked = await get_blocked_tags()
    for tag in res_tags:
        if tag in blocked:
            logger.info("Пропуск новости по тегу '%s': %s", tag, item["title"])
            return None
    return res_summary


async def send_for_approval(bot, item, processed_text, pending_id):
    keyboard = [[
        InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve_{pending_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{pending_id}"),
    ]]
    markup = InlineKeyboardMarkup(keyboard)

    image_url = item.get("image_url")
    image_prefix = f'<a href="{html.escape(image_url)}">&#8203;</a>' if image_url else ""
    safe_url = html.escape(item["url"])
    text = f"{image_prefix}📋 <b>Новая новость на проверку:</b>\n\n{processed_text}\n\n🔗 {safe_url}"

    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        logger.error("Error sending to admin (HTML): %s", e)
        try:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=f"📋 Новость на проверку (ошибка HTML):\n\n{item['title']}\n\n{item['url']}",
                reply_markup=markup,
            )
        except Exception:
            logger.error("Failed to send even simple text to admin")


async def fetch_news_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Fetching news...")
    news = await fetch_news()
    count = 0
    for item in news[:15]:
        processed_text = await process_and_filter_news(context.bot, item)
        if not processed_text:
            continue

        pending_id = await save_pending(item["title"], item["url"], processed_text, item.get("image_url"))
        await send_for_approval(context.bot, item, processed_text, pending_id)
        count += 1
        await asyncio.sleep(2)
        if count >= 8:
            break

    gc.collect()


async def post_to_channel(bot, pending_id):
    item = await get_pending(pending_id)
    if not item:
        return
    _, title, url, processed_text, image_url, _ = item

    image_prefix = f'<a href="{html.escape(image_url)}">&#8203;</a>' if image_url else ""
    safe_url = html.escape(url)
    text = f"{image_prefix}{processed_text}\n\n🔗 {safe_url}"

    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="HTML")
        await mark_posted(url)
        await log_event("post_approved", {"title": title, "url": url})
        logger.info("Posted: %s", title)
    except Exception as e:
        logger.error("Error posting to channel (HTML): %s", e)
        try:
            await bot.send_message(chat_id=CHANNEL_ID, text=f"{processed_text}\n\n{url}")
            await mark_posted(url)
            await log_event("post_approved", {"title": title, "url": url})
        except Exception:
            logger.error("Failed to post even simple text to channel")


async def send_weekly_digest(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Generating weekly digest...")
    stats = await get_weekly_stats()

    uptime = get_uptime()
    today = datetime.now()
    week_ago = today - timedelta(days=6)
    date_range = f"{week_ago.strftime('%d.%m')} — {today.strftime('%d.%m')}"

    text = (
        f"📊 <b>Итоги недели ({date_range}):</b>\n\n"
        f"📰 Новостей опубликовано: <code>{stats['approved']}</code>\n"
        f"✅ Одобрено: {stats['approved']} | ❌ Отклонено: {stats['rejected']}\n\n"
        f"⏱ Uptime: <code>{uptime}</code>"
    )

    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
    except Exception as e:
        logger.error("Error sending weekly digest: %s", e)
    finally:
        gc.collect()
