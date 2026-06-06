import asyncio
import logging
import aiosqlite
import html
import subprocess
import psutil
import json
import feedparser
import httpx
import gc
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import trade_engine

logger = logging.getLogger(__name__)
from config import (
    DB_PATH,
    ADMIN_ID,
    CHANNEL_ID,
    TRADE_PAIRS,
    GEMINI_MIN_CONFIDENCE,
    MAX_DAILY_TRADES,
)
from database import (
    is_posted, is_pending, mark_posted, save_pending, get_pending, delete_pending,
    log_event, get_blocked_tags, get_rss_feeds, get_gaming_keywords,
    get_open_position, save_trade, set_trade_state, get_trade_state,
    get_pending_follow_ups, mark_follow_up_sent,
    get_recent_sentiments, get_weekly_stats, get_daily_trades
)

from ai.news import process_with_gemini
from ai.trading import evaluate_trade_with_gemini, generate_daily_analytics

async def restart_bot(update: Update = None, context: ContextTypes.DEFAULT_TYPE = None):
    """Отправляет сообщение о перезапуске и инициирует его через systemctl."""
    msg_text = "🔄 <b>Инициирую перезапуск через systemd...</b>"
    if update and update.message:
        await update.message.reply_text(msg_text, parse_mode="HTML")
    elif context:
        await context.bot.send_message(chat_id=ADMIN_ID, text=msg_text, parse_mode="HTML")
    
    # Небольшая задержка, чтобы сообщение успело уйти
    await asyncio.sleep(1)
    import subprocess
    subprocess.Popen(["sudo", "systemctl", "restart", "gamebot.service"])
    import os
    os._exit(0)

async def check_job_follow_ups(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет вакансии, на которые вы откликнулись 7 дней назад."""
    pending = await get_pending_follow_ups(days=7)
    if not pending:
        return

    for vid, title, company, url, applied_at in pending:
        text = (
            f"🔔 <b>Пора спросить фидбек!</b>\n\n"
            f"Прошло 7 дней с вашего отклика на вакансию:\n"
            f"📦 <b>{html.escape(title)}</b>\n"
            f"🏢 {html.escape(company)}\n"
            f"📅 Дата отклика: {applied_at[:10]}\n\n"
            f"🔗 {html.escape(url)}"
        )
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
            await mark_follow_up_sent(vid)
        except Exception as e:
            logger.error(f"Error sending follow-up for {vid}: {e}")

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
    # interval=None не блокирует поток
    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    temp = get_cpu_temp()
    _, throttled = get_throttled_data()
    uptime = get_uptime()
    
    # Используем available вместо used для более точного понимания свободной памяти на Linux
    ram_used = (ram.total - ram.available) // (1024 * 1024)
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

# --- News Jobs ---
async def is_gaming(text):
    text_lower = text.lower()
    keywords = await get_gaming_keywords()
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

async def fetch_full_content(client, url):
    """Пытается получить полный текст статьи, если RSS-описание слишком короткое."""
    try:
        # Устанавливаем таймаут чуть больше для парсинга
        r = await client.get(url, timeout=12)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        
        # Удаляем мусор
        for s in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'form', 'iframe']):
            s.decompose()
            
        # Собираем осмысленные абзацы
        paragraphs = soup.find_all('p')
        text = "\n".join([p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 30])
        
        if len(text) < 100: # Если совсем мало текста, попробуем поискать в div.content и т.п. (упрощенно)
            content = soup.find(['article', 'main', 'div[class*="content"]'])
            if content:
                text = content.get_text(separator="\n").strip()
        
        return text[:4000] # Больше Gemini и не надо
    except Exception as e:
        logger.debug(f"Scrape error for {url}: {e}")
        return ""

async def fetch_single_feed(client, feed_url, keywords):
    try:
        response = await client.get(feed_url, timeout=10)
        response.raise_for_status()
        # parse теперь работает со строкой, а не делает сетевой запрос сам
        feed = feedparser.parse(response.text)
        results = []
        for entry in feed.entries[:5]:
            title = entry.get("title", "")
            url = entry.get("link", "")
            summary = entry.get("summary", "")
            if not url or await is_posted(url) or await is_pending(url):
                continue
            
            text_to_check = (title + " " + summary).lower()
            if keywords and not any(kw in text_to_check for kw in keywords):
                continue
            
            # Если описание короткое, попробуем вытянуть больше данных из самой статьи
            if len(summary) < 400:
                full_text = await fetch_full_content(client, url)
                if full_text and len(full_text) > len(summary):
                    summary = full_text
                
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

    # Семафор на 3 одновременных запроса, чтобы не забивать память
    sem = asyncio.Semaphore(3)

    async def sem_fetch(client, url, keywords):
        async with sem:
            return await fetch_single_feed(client, url, keywords)

    async with httpx.AsyncClient(headers={"User-Agent": "GameBot/1.0"}, timeout=15) as client:
        tasks = [sem_fetch(client, url, keywords) for url in feeds]
        results = await asyncio.gather(*tasks)
    
    news = [item for sublist in results for item in sublist]
    seen_urls = set()
    unique_news = []
    for item in news:
        if item['url'] not in seen_urls:
            unique_news.append(item)
            seen_urls.add(item['url'])
    
    gc.collect() # Очистка после сбора новостей
    return unique_news

async def process_and_filter_news(bot, item):
    result = await process_with_gemini(item["title"], item["summary"])
    res_summary = result.get("summary", "")
    res_tags = [t.lower() for t in result.get("tags", [])]
    sentiment = result.get("sentiment", 0)

    # Сохраняем сентимент в мета-данные события для истории
    await log_event("news_sentiment", {"url": item["url"], "sentiment": sentiment})

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
    # Берем больше новостей на проверку (до 15), но не больше 8 за раз
    for item in news[:15]:
        processed_text = await process_and_filter_news(context.bot, item)
        if not processed_text:
            continue

        pending_id = await save_pending(item["title"], item["url"], processed_text, item.get("image_url"))
        await send_for_approval(context.bot, item, processed_text, pending_id)
        count += 1
        await asyncio.sleep(2)
        if count >= 8: break
    
    gc.collect()

async def trade_job(context: ContextTypes.DEFAULT_TYPE):
    """Основной цикл трейдинга: для каждой пары OHLCV -> Indicators -> Signal -> Execute."""
    for pair in TRADE_PAIRS:
        logger.info(f"Starting trade cycle for {pair}...")
        
        # 1. Получаем данные
        df = await trade_engine.fetch_ohlcv(pair)
        if df is None:
            logger.error(f"Failed to fetch OHLCV data for {pair}")
            continue
            
        # 2. Считаем индикаторы
        df = trade_engine.calc_indicators(df)
        
        # 3. Получаем средний сентимент за последние 12 часов
        avg_sentiment = 0
        rows = await get_recent_sentiments(12)
        if rows:
            sentiments = []
            for r in rows:
                try:
                    if r[0]:
                        data = json.loads(r[0])
                        sentiments.append(data.get('sentiment', 0))
                except:
                    continue
            if sentiments:
                avg_sentiment = sum(sentiments) / len(sentiments)
        
        if df is None or df.empty:
            logger.warning(f"No data for indicators for {pair}, skipping")
            continue
            
        last_row = df.iloc[-1]
        last_price = last_row['close']
        last_atr = last_row['atr']
        last_adx = last_row['adx']
        
        current_pos = await get_open_position(pair)
        entry_price = await get_trade_state("entry_price", pair)
        highest_price = await get_trade_state("highest_price", pair)
        
        # Обновляем максимальную цену для трейлинг-стопа
        if current_pos == "in_position" and last_price is not None:
            if highest_price is None or float(last_price) > float(highest_price):
                highest_price = float(last_price)
                await set_trade_state("highest_price", highest_price, pair)

        risk_exit_reason = None
        if current_pos == "in_position" and entry_price is not None:
            try:
                risk_exit_reason = trade_engine.get_risk_exit_signal(
                    float(last_price), 
                    float(entry_price), 
                    float(last_atr),
                    highest_price=float(highest_price) if highest_price else None
                )
            except (TypeError, ValueError) as e:
                logger.warning(f"Error calculating risk exit for {pair}: {e}")
        
        # 4. Генерируем техсигнал
        technical_signal = trade_engine.get_signal(df, sentiment=avg_sentiment)
        if risk_exit_reason:
            technical_signal = "SELL"
        
        logger.info(f"[{pair}] Tech: {technical_signal} | ADX: {last_adx:.1f} | ATR: {last_atr:.4f} | RiskExit: {risk_exit_reason}")

        # 5. Оптимизация Gemini: вызываем только если есть техсигнал или нужно подтверждение выхода
        market_snapshot = {
            "pair": pair,
            "price": round(float(last_price), 4),
            "volume": round(float(last_row["volume"]), 2),
            "ema_fast": round(float(last_row["ema_fast"]), 4),
            "ema_slow": round(float(last_row["ema_slow"]), 4),
            "ema_trend": round(float(last_row["ema_trend"]), 4),
            "rsi": round(float(last_row["rsi"]), 2),
            "adx": round(float(last_adx), 2),
            "atr": round(float(last_atr), 4),
            "ema_gap": round(float(last_row["ema_fast"] - last_row["ema_slow"]), 4),
            "technical_signal": technical_signal,
            "position_state": current_pos or "none",
            "entry_price": round(float(entry_price), 4) if entry_price is not None else None,
            "highest_price": round(float(highest_price), 4) if highest_price is not None else None,
            "risk_exit": risk_exit_reason,
            "recent_candles": df[['close', 'volume', 'rsi']].tail(3).to_dict('records')
        }

        should_call_gemini = (technical_signal != "HOLD") or (risk_exit_reason is not None)
        
        if not should_call_gemini:
            gemini_decision = {
                "action": "HOLD",
                "confidence": 0.0,
                "reason": "Технических сигналов нет, ADX низкий — экономим API",
            }
            signal = "HOLD"
        elif risk_exit_reason:
            gemini_decision = {
                "action": "SELL",
                "confidence": 1.0,
                "reason": f"Сработал динамический выход: {risk_exit_reason}",
            }
            signal = "SELL"
        else:
            gemini_decision = await evaluate_trade_with_gemini(
                pair=pair,
                market_snapshot=market_snapshot,
                technical_signal=technical_signal,
                avg_sentiment=avg_sentiment,
            )
            
            # Консервативная логика принятия решения
            if technical_signal == "BUY":
                if gemini_decision["action"] == "BUY" and gemini_decision["confidence"] >= 0.6:
                    signal = "BUY"
                else:
                    signal = "HOLD"
            elif technical_signal == "SELL":
                # Если Gemini против продажи и уверенность > 0.8 — можем придержать
                if gemini_decision["action"] == "BUY" and gemini_decision["confidence"] >= 0.8:
                    signal = "HOLD"
                else:
                    signal = "SELL"
            else:
                signal = "HOLD"

        await set_trade_state("last_trade_signal", technical_signal, pair)
        await set_trade_state("last_gemini_action", gemini_decision["action"], pair)
        await set_trade_state("last_gemini_confidence", gemini_decision["confidence"], pair)
        await set_trade_state("last_gemini_reason", gemini_decision["reason"], pair)
        await set_trade_state("last_trade_decision", signal, pair)
        await set_trade_state("last_risk_exit_reason", risk_exit_reason, pair)
        
        logger.info(
            f"[{pair}] Final Decision: {signal} | Price: {last_price} | Gemini: {gemini_decision['action']} ({gemini_decision['confidence']})"
        )
        
        if signal == "BUY":
            if current_pos == "in_position":
                continue
                
            # Проверка лимитов перед входом
            daily_trades = await get_daily_trades(24)
            buy_trades_count = len([t for t in daily_trades if t[1] == 'BUY'])
            total_daily_pnl = sum([float(t[4]) for t in daily_trades if t[4] is not None])
            
            if buy_trades_count >= MAX_DAILY_TRADES:
                logger.warning(f"Daily trade limit reached ({MAX_DAILY_TRADES}). Skipping BUY for {pair}.")
                continue
                
            if total_daily_pnl < -100: # Условный стоп по дневному убытку в USDT (можно в конфиг)
                logger.warning(f"Daily drawdown limit reached. Skipping BUY for {pair}.")
                continue

        if signal == "SELL" and (current_pos == "none" or current_pos is None):
            continue
            
        if signal == "HOLD":
            continue
            
        trade_result = await trade_engine.execute_trade(signal, last_price, pair, avg_sentiment, atr=last_atr)

        
        if trade_result and trade_result.get("success"):
            # Сбрасываем highest_price при закрытии сделки
            if signal == "SELL":
                await set_trade_state("highest_price", None, pair)
            side_emoji = "🚀" if signal == "BUY" else "🔻"
            side_text = "ПОКУПКА" if signal == "BUY" else "ПРОДАЖА"
            pnl_text = ""
            
            exec_price = trade_result.get("price", last_price)
            exec_qty = trade_result.get("qty", 0)
            total_amount = exec_price * exec_qty

            if signal == "SELL":
                pnl = trade_result.get("pnl", 0.0)
                entry_p = trade_result.get("entry_price")
                if entry_p:
                    pnl_pct = (exec_price - entry_p) / entry_p * 100
                    plus_minus = "+" if pnl > 0 else ""
                    pnl_text = (
                        f"\nВход: <code>{entry_p:.2f}</code>"
                        f"\nРезультат: <b>{plus_minus}{pnl:.2f} USDT ({plus_minus}{pnl_pct:.2f}%)</b>"
                    )
                else:
                    pnl_text = f"\nРезультат: <b>{pnl:.2f} USDT</b>"

            text = (
                f"{side_emoji} <b>{side_text}: {pair}</b>\n\n"
                f"Цена {'входа' if signal == 'BUY' else 'выхода'}: <code>{exec_price}</code>\n"
                f"Объем: <code>{exec_qty}</code>\n"
                f"Сумма: <code>{total_amount:.2f} USDT</code>\n"
                f"Gemini: <code>{gemini_decision['action']}</code> ({gemini_decision['confidence']:.2f})\n"
                f"Причина: <code>{html.escape(gemini_decision['reason'])}</code>{pnl_text}\n"
                f"Режим: {'🧪 PAPER' if trade_engine.PAPER_MODE else '💰 LIVE'}"
            )
            
            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Failed to send trade notification for {pair}: {e}")

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
        logger.error(f"Error sending weekly digest: {e}")
    finally:
        gc.collect()

async def send_daily_trade_analytics(update: Update | ContextTypes.DEFAULT_TYPE, context: ContextTypes.DEFAULT_TYPE = None):
    """Собирает сделки за 24 часа и отправляет аналитику от Gemini."""
    # Если это вызов из JobQueue, то первый аргумент - это context, и у него нет атрибута 'update_id'
    if not hasattr(update, 'update_id'):
        context = update
        update = None
    
    logger.info("Generating daily trade analytics...")
    
    rows = await get_daily_trades(24)
            
    if not rows:
        logger.info("No trades today, sending empty status report.")
        header = (
            f"💰 <b>Статистика за 24ч:</b>\n"
            f"PnL: <code>0.00 USDT</code>\n"
            f"Сделок: <code>0</code>\n"
            f"Статус: <i>Активен, сигналов на вход не было.</i>"
        )
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=header, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Error sending empty daily analytics: {e}")
        return

    trades_summary = []
    total_pnl = 0.0
    wins = 0
    losses = 0

    for r in rows:
        pnl = float(r[4]) if r[4] is not None else 0.0
        total_pnl += pnl
        if pnl > 0: wins += 1
        elif pnl < 0: losses += 1
        
        trades_summary.append({
            "pair": r[0],
            "side": r[1],
            "price": r[2],
            "qty": r[3],
            "pnl": round(pnl, 2),
            "signal": r[5],
            "sentiment": r[6],
            "time": r[7]
        })

    # Добавляем сухую статистику в начало
    winrate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    
    # Запрашиваем "умный" разбор у Gemini
    ai_report = await generate_daily_analytics(trades_summary)
    
    header = (
        f"💰 <b>Статистика за 24ч:</b>\n"
        f"PnL: <code>{total_pnl:.2f} USDT</code>\n"
        f"Сделок: <code>{len(trades_summary)}</code> (W:{wins} / L:{losses})\n"
        f"Winrate: <code>{winrate:.1f}%</code>\n\n"
    )
    
    full_report = header + ai_report
    
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=full_report, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error sending daily analytics: {e}")

async def backup_db_job(context: ContextTypes.DEFAULT_TYPE):
    """Отправляет резервную копию базы данных админу."""
    logger.info("Creating database backup...")
    try:
        # Используем sqlite3 для корректного создания копии (VACUUM INTO или просто копирование файла)
        # Для простоты и безопасности на RPi просто копируем файл
        backup_path = f"{DB_PATH}.backup"
        import shutil
        shutil.copy2(DB_PATH, backup_path)
        
        with open(backup_path, 'rb') as f:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=f,
                filename=f"bot_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db",
                caption=f"📦 Резервная копия базы данных\n📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
            )
        
        # Удаляем временный файл
        import os
        os.remove(backup_path)
        logger.info("Backup sent successfully.")
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"❌ Ошибка при создании бэкапа: {e}")
