import asyncio
import logging
import aiosqlite
import html
import subprocess
import psutil
import json
import feedparser
import httpx
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import price_monitor
from config import DB_PATH, ADMIN_ID, CHANNEL_ID
from database import (
    get_watches, is_posted, is_pending, mark_posted, save_price_alert, 
    update_watch_price, save_pending, get_pending, delete_pending, 
    log_event, get_blocked_tags, get_rss_feeds, get_gaming_keywords
)
from ai_utils import process_with_gemini

logger = logging.getLogger(__name__)

TEMP_WARN = 65.0
TEMP_CRIT = 75.0
already_alerted = {"temp": False, "undervoltage": False}

# --- RPi Health Helpers ---
def get_cpu_temp():
    try:
        result = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True, text=True)
        temp_str = result.stdout.strip().replace("temp=", "").replace("'C", "")
        return float(temp_str)
    except:
        return None

def get_throttled_data():
    try:
        result = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True)
        val = result.stdout.strip().replace("throttled=", "")
        code = int(val, 16)
        flags = {
            "undervoltage": bool(code & 0x1),
            "frequency_capped": bool(code & 0x2),
            "throttling": bool(code & 0x4)
        }
        
        desc = []
        if flags["undervoltage"]: desc.append("⚡ Undervoltage!")
        if flags["frequency_capped"]: desc.append("🔻 Частота урезана")
        if flags["throttling"]: desc.append("🌡 Троттлинг")
        
        if code == 0:
            desc_str = "✅ Всё норм"
        elif not desc:
            desc_str = "📜 Исторические флаги (" + val + ")"
        else:
            desc_str = " | ".join(desc)
            
        return flags, desc_str
    except:
        return {}, "N/A"

def get_uptime():
    try:
        result = subprocess.run(["uptime", "-p"], capture_output=True, text=True)
        return result.stdout.strip()
    except:
        return "N/A"

def get_stats():
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    temp = get_cpu_temp()
    _, throttled = get_throttled_data()
    uptime = get_uptime()
    ram_used = ram.used // (1024 * 1024)
    ram_total = ram.total // (1024 * 1024)
    disk_used = disk.used // (1024 * 1024 * 1024)
    disk_total = disk.total // (1024 * 1024 * 1024)
    temp_display = f"{temp}°C" if temp is not None else "N/A"
    return (
        f"🖥 <b>Состояние малинки</b>\n\n"
        f"🌡 Температура: <code>{temp_display}</code>\n"
        f"⚙️ CPU: <code>{cpu}%</code>\n"
        f"💾 RAM: <code>{ram_used} / {ram_total} MB</code> ({ram.percent}%)\n"
        f"💿 Диск: <code>{disk_used} / {disk_total} GB</code> ({disk.percent}%)\n"
        f"⏱ Uptime: <code>{uptime}</code>\n"
        f"🔋 Питание: <code>{throttled}</code>"
    )

# --- Jobs ---
async def check_health_alert(context: ContextTypes.DEFAULT_TYPE):
    temp = get_cpu_temp()
    flags, _ = get_throttled_data()
    uv = flags.get("undervoltage", False)
    
    await log_event("health", {"temp": temp, "uv": uv})
    
    if temp is not None:
        if temp >= TEMP_CRIT and not already_alerted["temp"]:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"🔴 <b>КРИТИЧНО: температура {temp}°C!</b>", parse_mode="HTML")
            already_alerted["temp"] = True
        elif temp >= TEMP_WARN and not already_alerted["temp"]:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"🟡 <b>Предупреждение: температура {temp}°C</b>", parse_mode="HTML")
            already_alerted["temp"] = True
        elif temp < TEMP_WARN - 5:
            already_alerted["temp"] = False

    if flags.get("undervoltage") and not already_alerted["undervoltage"]:
        await context.bot.send_message(chat_id=ADMIN_ID, text="⚡ <b>Undervoltage detected!</b> Проверьте блок питания.", parse_mode="HTML")
        already_alerted["undervoltage"] = True
    elif not flags.get("undervoltage"):
        already_alerted["undervoltage"] = False

async def price_check_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Checking prices...")
    watches = await get_watches()
    for watch in watches:
        watch_id, user_id, name, url, target_price, last_price, service = watch
        current_price, srv_type, items = await price_monitor.check_price(url)
        
        if items: # Категория
            new_items_count = 0
            for item in items:
                if item['price'] <= target_price:
                    if not await is_posted(item['url']):
                        await save_price_alert(watch_id, item['price'], item['name'], item['url'])
                        await mark_posted(item['url'])
                        new_items_count += 1
            
            if new_items_count > 0:
                logger.info(f"Saved {new_items_count} alerts for {name}")
            
            if current_price is not None:
                await update_watch_price(watch_id, current_price)
        
        elif current_price is not None: # Одиночный товар
            if last_price is None or current_price != last_price:
                await update_watch_price(watch_id, current_price)
                
                if current_price <= target_price:
                    await save_price_alert(watch_id, current_price, name, url)
                    safe_name = html.escape(name)
                    safe_service = html.escape(srv_type or service)
                    safe_url = html.escape(url)
                    text = (f"🎯 <b>Цена упала!</b> ({safe_service})\n\n"
                            f"📦 {safe_name}\n"
                            f"💰 Текущая цена: <code>{current_price}</code>\n"
                            f"📉 Цель: <code>{target_price}</code>\n\n"
                            f"🔗 {safe_url}")
                    try:
                        await context.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
                        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
                            await db.execute("PRAGMA journal_mode=WAL")
                            await db.execute("UPDATE price_alerts SET sent_at=CURRENT_TIMESTAMP WHERE watch_id=? AND sent_at IS NULL", (watch_id,))
                            await db.commit()
                    except Exception as e:
                        logger.error(f"Error sending drop notification: {e}")
        await asyncio.sleep(2)

async def send_price_digest(context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        async with db.execute("""
            SELECT pa.id, pa.watch_id, pa.price, pa.item_name, pa.item_url, w.name as watch_name, w.user_id 
            FROM price_alerts pa
            JOIN watches w ON pa.watch_id = w.id
            WHERE pa.sent_at IS NULL
        """) as cursor:
            alerts = await cursor.fetchall()
    
    if not alerts:
        return

    by_watch = {}
    for alert in alerts:
        aid, wid, price, name, url, wname, uid = alert
        if wid not in by_watch:
            by_watch[wid] = {"name": wname, "uid": uid, "items": [], "ids": []}
        by_watch[wid]["items"].append({"name": name, "price": price, "url": url})
        by_watch[wid]["ids"].append(aid)

    for wid, data in by_watch.items():
        items_text = []
        for item in data["items"]:
            safe_name = html.escape(item["name"])
            safe_url = html.escape(item["url"])
            items_text.append(f"📦 {safe_name}\n💰 Цена: <code>{item['price']}</code>\n🔗 {safe_url}")
        
        text = f"🎁 <b>Новые находки по запросу: {html.escape(data['name'])}</b> ({len(data['items'])} шт.)\n\n" + "\n\n".join(items_text)
        
        try:
            await context.bot.send_message(chat_id=data["uid"], text=text, parse_mode="HTML", disable_web_page_preview=True)
            async with aiosqlite.connect(DB_PATH, timeout=30) as db:
                await db.execute("PRAGMA journal_mode=WAL")
                placeholders = ",".join(["?"] * len(data["ids"]))
                await db.execute(f"UPDATE price_alerts SET sent_at=CURRENT_TIMESTAMP WHERE id IN ({placeholders})", data["ids"])
                await db.commit()
        except Exception as e:
            logger.error(f"Error sending digest: {e}")

# --- News Jobs ---
async def is_gaming(text):
    text_lower = text.lower()
    keywords = await get_gaming_keywords()
    # Если база пуста, считаем всё игровым (или можно оставить дефолт)
    if not keywords:
        return True 
    return any(kw in text_lower for kw in keywords)

def get_image_from_entry(entry):
    if hasattr(entry, "media_content") and entry.media_content:
        return entry.media_content[0].get("url")
    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            if "image" in enc.get("type", ""):
                return enc.get("href")
    summary = entry.get("summary", "")
    if 'src="' in summary:
        start = summary.find('src="') + 5
        end = summary.find('"', start)
        return summary[start:end]
    return None

async def fetch_single_feed(client, feed_url, keywords):
    try:
        response = await client.get(feed_url, timeout=10)
        response.raise_for_status()
        feed = feedparser.parse(response.text)
        results = []
        for entry in feed.entries[:5]:
            title = entry.get("title", "")
            url = entry.get("link", "")
            summary = entry.get("summary", "")
            if not url or await is_posted(url) or await is_pending(url):
                continue
            
            # Проверка ключевых слов
            text_to_check = (title + " " + summary).lower()
            if keywords and not any(kw in text_to_check for kw in keywords):
                continue
                
            image_url = get_image_from_entry(entry)
            results.append({"title": title, "url": url, "summary": summary, "image_url": image_url})
        return results
    except Exception as e:
        logger.error(f"Feed error {feed_url}: {e}")
        return []

async def fetch_news():
    feeds = await get_rss_feeds()
    keywords = await get_gaming_keywords()
    
    if not feeds:
        logger.warning("No RSS feeds configured in database.")
        return []

    async with httpx.AsyncClient(headers={"User-Agent": "GameBot/1.0"}) as client:
        tasks = [fetch_single_feed(client, url, keywords) for url in feeds]
        results = await asyncio.gather(*tasks)
    
    news = [item for sublist in results for item in sublist]
    seen_urls = set()
    unique_news = []
    for item in news:
        if item['url'] not in seen_urls:
            unique_news.append(item)
            seen_urls.add(item['url'])
    return unique_news

async def process_and_filter_news(bot, item):
    result = await process_with_gemini(item["title"], item["summary"])
    res_summary = result.get("summary", "")
    res_tags = [t.lower() for t in result.get("tags", [])]

    blocked = await get_blocked_tags()
    for tag in res_tags:
        if tag in blocked:
            logger.info(f"Пропуск новости по тегу '{tag}': {item['title']}")
            return None
    return res_summary

async def send_for_approval(bot, item, processed_text, pending_id):
    keyboard = [[
        InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve_{pending_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{pending_id}")
    ]]
    markup = InlineKeyboardMarkup(keyboard)
    
    image_url = item.get("image_url")
    image_prefix = f'<a href="{html.escape(image_url)}">&#8203;</a>' if image_url else ""
    safe_url = html.escape(item['url'])
    text = f"{image_prefix}📋 <b>Новая новость на проверку:</b>\n\n{processed_text}\n\n🔗 {safe_url}"
    
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        logger.error(f"Error sending to admin (HTML): {e}")
        try:
            await bot.send_message(chat_id=ADMIN_ID, text=f"📋 Новость на проверку (ошибка HTML):\n\n{item['title']}\n\n{item['url']}", reply_markup=markup)
        except:
            logger.error("Failed to send even simple text to admin")

async def fetch_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Fetching news...")
    news = await fetch_news()
    count = 0
    for item in news[:5]:
        processed_text = await process_and_filter_news(context.bot, item)
        if not processed_text:
            continue

        pending_id = await save_pending(item["title"], item["url"], processed_text, item.get("image_url"))
        await send_for_approval(context.bot, item, processed_text, pending_id)
        count += 1
        await asyncio.sleep(2)
        if count >= 3: break

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
        logger.info(f"Posted: {title}")
    except Exception as e:
        logger.error(f"Error posting to channel (HTML): {e}")
        try:
            await bot.send_message(chat_id=CHANNEL_ID, text=f"{processed_text}\n\n{url}")
            await mark_posted(url)
            await log_event("post_approved", {"title": title, "url": url})
        except:
            logger.error("Failed to post even simple text to channel")

async def send_weekly_digest(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Generating weekly digest...")
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        
        seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        
        async with db.execute("SELECT COUNT(*) FROM events_log WHERE event_type='post_approved' AND created_at > ?", (seven_days_ago,)) as cursor:
            row = await cursor.fetchone()
            approved = row[0]
        async with db.execute("SELECT COUNT(*) FROM events_log WHERE event_type='post_rejected' AND created_at > ?", (seven_days_ago,)) as cursor:
            row = await cursor.fetchone()
            rejected = row[0]
        
        async with db.execute("SELECT COUNT(*) FROM watches") as cursor:
            row = await cursor.fetchone()
            watches_count = row[0]
        async with db.execute("SELECT COUNT(*) FROM events_log WHERE event_type='price_alert' AND created_at > ?", (seven_days_ago,)) as cursor:
            row = await cursor.fetchone()
            alerts_count = row[0]
        
        async with db.execute("SELECT meta FROM events_log WHERE event_type='health' AND created_at > ?", (seven_days_ago,)) as cursor:
            health_logs = await cursor.fetchall()
    
    max_temp = 0
    uv_count = 0
    for log in health_logs:
        data = json.loads(log[0])
        temp = data.get("temp")
        if temp and temp > max_temp:
            max_temp = temp
        if data.get("uv"):
            uv_count += 1
            
    uptime = get_uptime()
    today = datetime.now()
    week_ago = today - timedelta(days=6)
    date_range = f"{week_ago.strftime('%d.%m')} — {today.strftime('%d.%m')}"
    
    text = (
        f"📊 <b>Итоги недели ({date_range}):</b>\n\n"
        f"📰 Новостей опубликовано: <code>{approved}</code>\n"
        f"✅ Одобрено: {approved} | ❌ Отклонено: {rejected}\n\n"
        f"🛍 Мониторится товаров: <code>{watches_count}</code>\n"
        f"🔔 Алертов по ценам: <code>{alerts_count}</code>\n\n"
        f"🌡 Макс. температура RPi: <code>{max_temp}°C</code>\n"
        f"⚡️ Undervoltage за неделю: <code>{uv_count}</code> раз\n"
        f"⏱ Uptime: <code>{uptime}</code>"
    )
    
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error sending weekly digest: {e}")
