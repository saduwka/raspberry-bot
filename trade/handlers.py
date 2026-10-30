import html
import io
import logging
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CommandHandler, ContextTypes

from config import PAPER_MODE, TRADE_PAIRS
from core.auth import admin_only
from trade import engine as trade_engine
from trade import strategy as st
from trade.market import fetch_prices
from trade.policy import PRESETS, format_policy, load_policy, set_aggression, silence_days_from
from trade.repo import clear_pair_state, get_closed_trades, get_last_trade_at, get_trade_state
from trade.wallet import format_money, format_wallet_message, get_wallet_summary, reset_wallet

logger = logging.getLogger(__name__)
ALMATY = timezone(timedelta(hours=5))


def _local(ts):
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return str(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ALMATY).strftime("%d.%m %H:%M")


def _with_trade_engine_cleanup(func):
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        finally:
            trade_engine.release_heavy_libs()
    return wrapper


async def _pause_button():
    from trade.jobs import trading_pause_status
    paused, _ = await trading_pause_status()
    return (InlineKeyboardButton("▶️ Возобновить", callback_data="trade_resume") if paused
            else InlineKeyboardButton("⏸ Пауза", callback_data="trade_pause"))


async def trade_keyboard(pair=None):
    active_pair = pair or TRADE_PAIRS[0]
    rows = []
    pair_row = []
    for p in TRADE_PAIRS:
        pair_row.append(InlineKeyboardButton(f"🎯 {p}" if p == active_pair and pair else p,
                                             callback_data=f"trade_select_{p}"))
        if len(pair_row) == 3:
            rows.append(pair_row)
            pair_row = []
    if pair_row:
        rows.append(pair_row)
    rows.append([
        InlineKeyboardButton("📂 Позиции", callback_data="trade_positions"),
        InlineKeyboardButton("📜 История", callback_data="trade_history"),
    ])
    rows.append([
        InlineKeyboardButton("📊 Статистика", callback_data="trade_perf"),
        InlineKeyboardButton("📈 График", callback_data="trade_chart"),
    ])
    rows.append([
        InlineKeyboardButton("💼 Кошелек", callback_data="trade_wallet"),
        InlineKeyboardButton("⚙️ Политика", callback_data="trade_policy"),
    ])
    rows.append([await _pause_button(), InlineKeyboardButton("🔄 Меню", callback_data="trade_menu")])
    return InlineKeyboardMarkup(rows)


def _back_markup(extra=None):
    rows = list(extra or [])
    rows.append([InlineKeyboardButton("⬅️ Меню трейдинга", callback_data="trade_menu")])
    return InlineKeyboardMarkup(rows)


async def _reply(update: Update, context, text, markup):
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return
            if "no text in the message" not in str(e).lower():
                logger.warning("edit_text failed: %s", e)
        await context.bot.send_message(update.effective_chat.id, text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def show_trade_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from trade.jobs import trading_pause_status

    prices = await fetch_prices(TRADE_PAIRS)
    s = await get_wallet_summary(prices)
    rate = s["usdt_kzt_rate"]
    paused, reason = await trading_pause_status()
    policy = await load_policy()
    lines = []
    for p in TRADE_PAIRS:
        sig = await get_trade_state("last_trade_signal", p) or "—"
        snap = await get_trade_state("last_snapshot", p) or {}
        in_pos = any(x["pair"] == p for x in s["positions"])
        trend = {"up": "↑", "down": "↓", "flat": "→"}.get(snap.get("htf_trend"), "?")
        price = prices.get(p) or snap.get("price")
        dist = snap.get("dist_to_breakout_pct")
        dist_txt = f", до пробоя {dist:+.2f}%" if isinstance(dist, (int, float)) and not in_pos else ""
        lines.append(f"{'💰' if in_pos else '•'} <b>{p}</b> <code>{price}</code> | 4h {trend} | {sig}{dist_txt}")
    status = f"⏸ <b>Пауза:</b> <i>{html.escape(reason)}</i>" if paused else "▶️ <b>Торговля активна</b>"
    text = (
        f"📈 <b>Трейдинг</b> — {'🧪 PAPER' if PAPER_MODE else '💰 LIVE'}, стратегия v2\n\n"
        f"💼 Equity: {format_money(s['equity_usdt'], rate, code=True)} | "
        f"PnL: <code>{format_money(s['total_pnl_usdt'], rate, signed=True)} ({s['roi_pct']:+.2f}%)</code>\n"
        f"💵 Свободно: {format_money(s['cash_usdt'], rate, code=True)} | "
        f"🎰 Слоты: <code>{s['positions_count']}/{s['max_positions']}</code>\n"
        f"⚙️ Агрессия: <code>{policy['aggression']}/5</code> | {status}\n\n"
        + "\n".join(lines)
    )
    await _reply(update, context, text, await trade_keyboard())


@_with_trade_engine_cleanup
async def trade_pair_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, pair=None):
    active_pair = pair or TRADE_PAIRS[0]
    policy = await load_policy()
    analysis = await trade_engine.analyze_pair(active_pair, policy=policy)
    if not analysis:
        await _reply(update, context, f"❌ Не удалось получить рыночные данные для {active_pair}.",
                     await trade_keyboard(active_pair))
        return
    snap = analysis["snapshot"]
    checks = "\n".join(f"{'✅' if ok_ else '▫️'} {html.escape(name)}" for name, ok_ in analysis["checks"])
    trend = {"up": "восходящий ↑", "down": "нисходящий ↓", "flat": "боковик →"}.get(analysis["htf_trend"], "нет данных")
    pos_text = ""
    if await get_trade_state("current_position", active_pair) == "in_position":
        prices = await fetch_prices([active_pair])
        price = prices.get(active_pair) or snap["price"]
        s = await get_wallet_summary({active_pair: price})
        p = next((x for x in s["positions"] if x["pair"] == active_pair), None)
        stop = await get_trade_state("current_stop", active_pair)
        stage = await get_trade_state("stop_stage", active_pair)
        if p:
            stop_line = (f"Стоп: <code>{float(stop):.4f}</code> "
                         f"({'трейлинг' if stage == 'trailing' else 'начальный'})\n") if stop else ""
            pos_text = (
                f"\n💼 <b>Позиция:</b> вход <code>{p['entry_price']:.4f}</code>, сейчас <code>{price:.4f}</code>\n"
                f"PnL: <code>{format_money(p['unrealized_pnl'], s['usdt_kzt_rate'], signed=True)} "
                f"({p['unrealized_pct']:+.2f}%)</code>\n{stop_line}"
            )
    reason = await get_trade_state("last_gemini_reason", active_pair)
    action = await get_trade_state("last_gemini_action", active_pair)
    conf = await get_trade_state("last_gemini_confidence", active_pair)
    decision = await get_trade_state("last_trade_decision", active_pair)
    model_text = ""
    if action:
        model_text = (
            f"\n🤖 <b>Последнее решение модели:</b> <code>{html.escape(str(action))}</code> "
            f"({float(conf or 0):.2f}) → <code>{html.escape(str(decision or '—'))}</code>\n"
            f"<i>{html.escape(str(reason or ''))}</i>\n"
        )
    text = (
        f"🧠 <b>{active_pair}</b> — свеча 1h от {_local(datetime.fromtimestamp(analysis['candle_ts'] / 1000, timezone.utc).isoformat())}\n\n"
        f"Цена: <code>{snap['price']}</code> | уровень пробоя: <code>{snap['breakout_level']}</code>\n"
        f"Тренд 4h: <b>{trend}</b>\n"
        f"ATR: <code>{snap['atr']}</code> ({snap['atr_pct']}%) | RSI: <code>{snap['rsi']}</code> | "
        f"ADX: <code>{snap['adx']}</code>\n\n"
        f"<b>Условия входа ({analysis['signal']}):</b>\n{checks}\n"
        f"{pos_text}{model_text}"
    )
    extra = []
    if pos_text:
        extra.append([InlineKeyboardButton(f"❌ Закрыть {active_pair}", callback_data=f"trade_close_{active_pair}")])
    await _reply(update, context, text, _back_markup(extra + [[
        InlineKeyboardButton("🔄 Обновить", callback_data=f"trade_select_{active_pair}")]]))


async def trade_positions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prices = await fetch_prices(TRADE_PAIRS)
    s = await get_wallet_summary(prices)
    rate = s["usdt_kzt_rate"]
    if not s["positions"]:
        await _reply(update, context, "📂 <b>Открытых позиций нет</b>\n\n<i>Бот ждёт пробоя в восходящем тренде 4h.</i>",
                     _back_markup())
        return
    blocks, buttons = [], []
    for p in s["positions"]:
        stop = await get_trade_state("current_stop", p["pair"])
        stage = await get_trade_state("stop_stage", p["pair"])
        stop_txt = (f"\nСтоп: <code>{float(stop):.4f}</code> ({'трейлинг' if stage == 'trailing' else 'начальный'}, "
                    f"{(float(stop) / p['cur_price'] - 1) * 100:+.2f}%)") if stop else ""
        blocks.append(
            f"💰 <b>{p['pair']}</b> с {_local(p.get('opened_at'))}\n"
            f"Вход <code>{p['entry_price']:.4f}</code> → <code>{p['cur_price']:.4f}</code>, объём <code>{p['qty']}</code>\n"
            f"PnL: <code>{format_money(p['unrealized_pnl'], rate, signed=True)} ({p['unrealized_pct']:+.2f}%)</code>"
            f"{stop_txt}"
        )
        buttons.append([InlineKeyboardButton(f"❌ Закрыть {p['pair']}", callback_data=f"trade_close_{p['pair']}")])
    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data="trade_positions")])
    await _reply(update, context, "📂 <b>Открытые позиции</b>\n\n" + "\n\n".join(blocks), _back_markup(buttons))


async def trade_close_confirm(update: Update, context, pair):
    await _reply(update, context, f"Закрыть <b>{pair}</b> по рынку сейчас?", InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, закрыть", callback_data=f"trade_closeyes_{pair}"),
        InlineKeyboardButton("Отмена", callback_data="trade_positions"),
    ]]))


async def trade_history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = await get_wallet_summary()
    rate = s["usdt_kzt_rate"]
    rows = await get_closed_trades(limit=12)
    if not rows:
        await _reply(update, context, "📜 <b>Закрытых сделок пока нет</b>", _back_markup())
        return
    lines = []
    for pair, price, qty, pnl, fee, entry, reason, signal, created in rows:
        pnl = float(pnl or 0)
        label = st.EXIT_LABELS.get(reason or "", (signal or "").replace("AGGRESSIVE_", "v1 ").lower())
        move = f" {(float(price) / float(entry) - 1) * 100:+.2f}%" if entry else ""
        lines.append(
            f"{'🟢' if pnl > 0 else '🔴'} {_local(created)} <b>{pair}</b>{move} → "
            f"<code>{format_money(pnl, rate, signed=True)}</code> <i>{html.escape(label)}</i>"
        )
    await _reply(update, context, "📜 <b>Последние закрытые сделки</b>\n\n" + "\n".join(lines),
                 _back_markup([[InlineKeyboardButton("📊 Статистика", callback_data="trade_perf")]]))


def _perf(rows):
    pnls = [float(r[3] or 0) for r in rows]
    if not pnls:
        return None
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    return {
        "n": len(pnls),
        "net": sum(pnls),
        "fees": sum(float(r[4] or 0) for r in rows),
        "wr": len(wins) / len(pnls) * 100,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "pf": sum(wins) / abs(sum(losses)) if losses else float("inf"),
    }


async def trade_perf_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = await get_wallet_summary()
    rate = s["usdt_kzt_rate"]
    blocks = []
    for label, kwargs in (("7 дней", {"days": 7}), ("30 дней", {"days": 30}),
                          ("с запуска кошелька", {"since": (s.get("created_at") or "")[:19].replace("T", " ")})):
        rows = await get_closed_trades(**kwargs)
        p = _perf(rows)
        if not p:
            blocks.append(f"📅 <b>{label}:</b> сделок нет")
            continue
        per_pair = {}
        for r in rows:
            per_pair[r[0]] = per_pair.get(r[0], 0.0) + float(r[3] or 0)
        pairs_txt = " | ".join(f"{k.split('/')[0]} {v:+.3f}" for k, v in per_pair.items())
        pf = "∞" if p["pf"] == float("inf") else f"{p['pf']:.2f}"
        blocks.append(
            f"📅 <b>{label}:</b> {p['n']} сделок, винрейт <code>{p['wr']:.0f}%</code>, PF <code>{pf}</code>\n"
            f"PnL: <code>{format_money(p['net'], rate, signed=True)}</code>, комиссии <code>{p['fees']:.3f}</code> USDT\n"
            f"Ср. плюс <code>{p['avg_win']:+.3f}</code> / ср. минус <code>{p['avg_loss']:+.3f}</code> USDT\n"
            f"<i>{pairs_txt}</i>"
        )
    text = ("📊 <b>Результаты (чистыми, USDT)</b>\n\n" + "\n\n".join(blocks) +
            "\n\n<i>Сделки до v2 записаны без комиссий. Бэктест v2 за год: PF ≈ 1.05, винрейт ≈ 33%.</i>")
    await _reply(update, context, text, _back_markup())


def _render_equity_png(points, initial, title):
    from PIL import Image, ImageDraw, ImageFont

    W, H, pad_l, pad_r, pad_t, pad_b = 900, 460, 80, 30, 60, 50
    img = Image.new("RGB", (W, H), (17, 20, 26))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        big = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
    except OSError:
        font = big = ImageFont.load_default()
    vals = [v for _, v in points]
    lo, hi = min(vals + [initial]), max(vals + [initial])
    if hi - lo < 1e-9:
        hi, lo = hi + 0.5, lo - 0.5
    span = hi - lo
    lo -= span * 0.1
    hi += span * 0.1

    def xy(i, v):
        x = pad_l + (W - pad_l - pad_r) * (i / max(1, len(points) - 1))
        y = pad_t + (H - pad_t - pad_b) * (1 - (v - lo) / (hi - lo))
        return x, y

    for k in range(5):
        v = lo + (hi - lo) * k / 4
        _, y = xy(0, v)
        d.line([(pad_l, y), (W - pad_r, y)], fill=(40, 45, 55))
        d.text((10, y - 9), f"{v:.2f}", fill=(150, 155, 165), font=font)
    _, y0 = xy(0, initial)
    d.line([(pad_l, y0), (W - pad_r, y0)], fill=(90, 95, 110), width=1)
    color = (46, 204, 113) if vals[-1] >= initial else (231, 76, 60)
    d.line([xy(i, v) for i, (_, v) in enumerate(points)], fill=color, width=3)
    d.text((pad_l, 18), title, fill=(230, 232, 235), font=big)
    d.text((pad_l, H - 35), _local(points[0][0]), fill=(150, 155, 165), font=font)
    end = _local(points[-1][0])
    d.text((W - pad_r - d.textlength(end, font=font), H - 35), end, fill=(150, 155, 165), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def trade_chart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = await get_wallet_summary(await fetch_prices(TRADE_PAIRS))
    since = (s.get("created_at") or "")[:19].replace("T", " ")
    rows = list(reversed(await get_closed_trades(since=since)))
    initial = s["initial_usdt"]
    points = [(s.get("created_at"), initial)]
    eq = initial
    for r in rows:
        eq += float(r[3] or 0)
        points.append((r[8], eq))
    points.append((datetime.now(timezone.utc).isoformat(), s["equity_usdt"]))
    title = f"Equity: {s['equity_usdt']:.2f} USDT ({s['roi_pct']:+.2f}%), сделок {len(rows)}"
    buf = _render_equity_png(points, initial, title)
    chat_id = update.effective_chat.id
    await context.bot.send_photo(chat_id, photo=buf, caption="📈 Кривая капитала (чистыми, с открытыми позициями)")


async def trade_policy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, pair=None):
    policy = await load_policy()
    silence_days = silence_days_from(await get_last_trade_at())
    text = (f"{format_policy(policy, silence_days)}\n\n"
            "<i>Агрессия меняет частоту входов (длина пробоя), строгость фильтра модели и лимит BUY/день. "
            "Стоп, трейлинг и дневной лимит убытка не отключаются.</i>")
    row = [InlineKeyboardButton(f"{'● ' if policy['aggression'] == lvl else ''}{lvl}",
                                callback_data=f"trade_aggr_{lvl}") for lvl in sorted(PRESETS)]
    await _reply(update, context, text, _back_markup([row]))


@_with_trade_engine_cleanup
async def trade_wallet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    summary = await get_wallet_summary(await fetch_prices(TRADE_PAIRS))
    await _reply(update, context, format_wallet_message(summary), _back_markup([[
        InlineKeyboardButton("🔄 Обновить", callback_data="trade_wallet"),
        InlineKeyboardButton("♻️ Сбросить", callback_data="trade_wallet_reset"),
    ]]))


async def trade_wallet_reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, confirmed=False):
    if not confirmed:
        await _reply(update, context,
                     "♻️ Сбросить виртуальный кошелек до начального депозита?\n"
                     "<i>Открытые позиции будут удалены без продажи, история сделок останется.</i>",
                     InlineKeyboardMarkup([[
                         InlineKeyboardButton("✅ Да, сбросить", callback_data="trade_wallet_reset_yes"),
                         InlineKeyboardButton("Отмена", callback_data="trade_wallet"),
                     ]]))
        return
    await reset_wallet()
    await clear_pair_state(TRADE_PAIRS)
    if update.callback_query:
        await update.callback_query.answer("✅ Кошелек сброшен")
    await trade_wallet_handler(update, context)


async def _pause(update, context, paused):
    from trade.jobs import set_manual_pause
    await set_manual_pause(paused)
    if update.callback_query:
        await update.callback_query.answer("⏸ Новые входы на паузе" if paused else "▶️ Торговля возобновлена")
        await show_trade_menu(update, context)
    else:
        await update.message.reply_text("⏸ Новые входы на паузе. Стопы открытых позиций работают."
                                        if paused else "▶️ Торговля возобновлена.")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    if not data.startswith("trade_"):
        return None
    query = update.callback_query
    if data == "trade_menu":
        await show_trade_menu(update, context)
    elif data.startswith("trade_select_") or data.startswith("trade_stats_") \
            or data.startswith("trade_signal_") or data.startswith("trade_refresh_"):
        pair = data.split("_", 2)[2]
        await trade_pair_handler(update, context, pair=pair)
    elif data == "trade_positions":
        await trade_positions_handler(update, context)
    elif data.startswith("trade_closeyes_"):
        from trade.jobs import close_position_manual
        ok_, msg = await close_position_manual(context.bot, data[len("trade_closeyes_"):])
        try:
            await query.answer(("✅ " if ok_ else "❌ ") + msg[:180], show_alert=not ok_)
        except BadRequest:
            pass
        await trade_positions_handler(update, context)
    elif data.startswith("trade_close_"):
        await trade_close_confirm(update, context, data[len("trade_close_"):])
    elif data == "trade_history":
        await trade_history_handler(update, context)
    elif data == "trade_perf":
        await trade_perf_handler(update, context)
    elif data == "trade_chart":
        await trade_chart_handler(update, context)
    elif data == "trade_policy":
        await trade_policy_handler(update, context)
    elif data.startswith("trade_aggr_"):
        level = int(data.rsplit("_", 1)[1])
        policy = await load_policy()
        _, msg = await set_aggression(policy, level, "изменено вручную в Telegram")
        try:
            await query.answer(f"⚙️ Агрессия {level}/5")
        except BadRequest:
            pass
        await trade_policy_handler(update, context)
    elif data == "trade_wallet" or data == "trade_wallet_refresh":
        await trade_wallet_handler(update, context)
    elif data == "trade_wallet_reset":
        await trade_wallet_reset_handler(update, context)
    elif data == "trade_wallet_reset_yes":
        await trade_wallet_reset_handler(update, context, confirmed=True)
    elif data == "trade_pause":
        await _pause(update, context, True)
    elif data == "trade_resume":
        await _pause(update, context, False)
    else:
        return None
    return True


@admin_only
async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await trade_positions_handler(update, context)


@admin_only
async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await trade_history_handler(update, context)


@admin_only
async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _pause(update, context, True)


@admin_only
async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _pause(update, context, False)


@admin_only
async def chart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await trade_chart_handler(update, context)


def register(app):
    app.add_handler(CommandHandler("positions", positions_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("trade_pause", pause_command))
    app.add_handler(CommandHandler("trade_resume", resume_command))
    app.add_handler(CommandHandler("trade_chart", chart_command))
