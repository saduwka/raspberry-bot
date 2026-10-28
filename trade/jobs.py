import html
import json
import logging
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ai.trading import evaluate_cycle, generate_daily_analytics
from config import (
    ADMIN_ID,
    ENTRY_COOLDOWN_SECONDS,
    LOSS_PAUSE_HOURS,
    MAX_CONSECUTIVE_LOSSES,
    MAX_DAILY_LOSS_PCT,
    PAPER_MODE,
    TRADE_PAIRS,
)
from core.html import ensure_valid_html
from news.repo import get_recent_sentiments
from trade import engine as trade_engine
from trade import strategy as st
from trade.market import fetch_prices
from trade.policy import (
    apply_policy_patch,
    format_policy,
    load_policy,
    load_trade_lessons,
    maybe_escalate_silence,
    review_closed_trade,
    silence_days_from,
)
from trade.repo import (
    clear_pair_state,
    get_daily_trades,
    get_last_trade_at,
    get_trade_state,
    set_trade_state,
)
from trade.wallet import format_money, get_wallet, get_wallet_summary

logger = logging.getLogger(__name__)

PAUSE_KEY = "trading_paused"
PAUSE_UNTIL_KEY = "trading_paused_until"
LOSS_STREAK_KEY = "loss_streak"


def _avg_sentiment(rows) -> float:
    vals = []
    for r in rows or []:
        try:
            if r[0]:
                vals.append(float(json.loads(r[0]).get("sentiment", 0)))
        except Exception:
            continue
    return sum(vals) / len(vals) if vals else 0.0


def _parse_ts(text):
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _in_cooldown(last_exit_at) -> bool:
    dt = _parse_ts(last_exit_at)
    return bool(dt and (datetime.now(timezone.utc) - dt).total_seconds() < ENTRY_COOLDOWN_SECONDS)


async def _notify(bot, text: str, reply_markup=None):
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", reply_markup=reply_markup)
    except BadRequest as e:
        logger.warning("HTML send failed (%s), retrying plain", e)
        try:
            await bot.send_message(chat_id=ADMIN_ID, text=text)
        except Exception as retry_err:
            logger.error("Failed to send trade message: %s", retry_err)
    except Exception as e:
        logger.error("Failed to send trade message: %s", e)


async def trading_pause_status():
    """(paused: bool, reason: str | None)."""
    if await get_trade_state(PAUSE_KEY, "GLOBAL"):
        return True, "ручная пауза"
    until = _parse_ts(await get_trade_state(PAUSE_UNTIL_KEY, "GLOBAL"))
    if until and until > datetime.now(timezone.utc):
        local = until.astimezone(timezone(timedelta(hours=5)))
        return True, f"серия убытков, пауза до {local:%d.%m %H:%M}"
    return False, None


async def set_manual_pause(paused: bool):
    await set_trade_state(PAUSE_KEY, bool(paused), "GLOBAL")
    if not paused:
        await set_trade_state(PAUSE_UNTIL_KEY, None, "GLOBAL")
        await set_trade_state(LOSS_STREAK_KEY, 0, "GLOBAL")


async def _reconcile():
    """Кошелек — источник правды о позициях; trade_state по парам синхронизируется с ним."""
    wallet = await get_wallet()
    positions = wallet.get("positions") or {}
    for pair in TRADE_PAIRS:
        cur = await get_trade_state("current_position", pair)
        pos = positions.get(pair)
        if pos and cur != "in_position":
            logger.warning("Reconcile: %s есть в кошельке, но не в trade_state — восстанавливаю", pair)
            await set_trade_state("current_position", "in_position", pair)
            await set_trade_state("entry_price", pos.get("entry_price"), pair)
            await set_trade_state("position_qty", pos.get("qty"), pair)
            await set_trade_state("highest_price", pos.get("entry_price"), pair)
            if pos.get("entry_atr"):
                await set_trade_state("entry_atr", pos.get("entry_atr"), pair)
        elif not pos and cur == "in_position":
            logger.warning("Reconcile: %s в trade_state без позиции в кошельке — очищаю", pair)
            await clear_pair_state([pair])
    return positions


async def _daily_loss_hit(initial_usdt: float, pnl_today: float) -> bool:
    return pnl_today < -(initial_usdt * MAX_DAILY_LOSS_PCT / 100.0)


async def _after_close(bot, policy, pair, result, reason, ai_note=None):
    rate = (await get_wallet_summary())["usdt_kzt_rate"]
    pnl = float(result.get("pnl") or 0.0)
    entry = float(result.get("entry_price") or 0.0)
    exit_p = float(result.get("exit_price") or 0.0)
    pct = (exit_p - entry) / entry * 100 if entry else 0.0
    held = ""
    opened = _parse_ts(result.get("opened_at"))
    if opened:
        hours = (datetime.now(timezone.utc) - opened).total_seconds() / 3600
        held = f"\nВ позиции: <code>{hours:.1f} ч</code>"
    wallet_sum = await get_wallet_summary()
    await _notify(bot, (
        f"🔻 <b>ПРОДАЖА: {pair}</b> — {html.escape(st.EXIT_LABELS.get(reason, reason))}\n\n"
        f"Вход: <code>{entry:.4f}</code> → выход: <code>{exit_p:.4f}</code> ({pct:+.2f}%)\n"
        f"Комиссии: {format_money(result.get('fee_total', 0), rate, code=True)}\n"
        f"Результат (чистый): <b>{format_money(pnl, rate, signed=True)}</b>{held}\n"
        f"{('🤖 ' + html.escape(ai_note) + chr(10)) if ai_note else ''}"
        f"💼 Свободно: {format_money(wallet_sum['cash_usdt'], rate, code=True)} | "
        f"Слоты: <code>{wallet_sum['positions_count']}/{wallet_sum['max_positions']}</code>\n"
        f"Режим: {'🧪 PAPER' if PAPER_MODE else '💰 LIVE'}"
    ))

    streak = int(await get_trade_state(LOSS_STREAK_KEY, "GLOBAL") or 0)
    streak = streak + 1 if pnl < 0 else 0
    await set_trade_state(LOSS_STREAK_KEY, streak, "GLOBAL")
    await set_trade_state("last_exit_at", datetime.now(timezone.utc).isoformat(), pair)
    if streak >= MAX_CONSECUTIVE_LOSSES:
        until = datetime.now(timezone.utc) + timedelta(hours=LOSS_PAUSE_HOURS)
        await set_trade_state(PAUSE_UNTIL_KEY, until.isoformat(), "GLOBAL")
        await set_trade_state(LOSS_STREAK_KEY, 0, "GLOBAL")
        await _notify(bot, (
            f"⏸ <b>{streak} убытка подряд</b> — новые входы на паузе {LOSS_PAUSE_HOURS:g} ч.\n"
            f"Стопы открытых позиций продолжают работать."
        ))

    trade_ctx = {
        "pair": pair,
        "entry_price": entry,
        "exit_price": exit_p,
        "qty": result.get("qty"),
        "pnl": pnl,
        "fees": result.get("fee_total"),
        "exit_reason": reason,
        "held_hours": round((datetime.now(timezone.utc) - opened).total_seconds() / 3600, 1) if opened else None,
    }
    policy, review_msg = await review_closed_trade(policy, trade_ctx)
    if review_msg:
        await _notify(bot, review_msg)
    return policy


async def close_position_manual(bot, pair):
    """Ручное закрытие из Telegram по текущей цене."""
    from trade.engine import live_price

    if await get_trade_state("current_position", pair) != "in_position":
        return False, "Позиция не открыта"
    price = await live_price(pair)
    if not price:
        return False, "Не удалось получить цену"
    result = await trade_engine.close_long(pair, price, "MANUAL")
    if not result.get("success"):
        return False, result.get("reason", "ошибка")
    await _after_close(bot, await load_policy(), pair, result, "MANUAL")
    return True, f"Закрыто по {result['exit_price']:.4f}, PnL {result['pnl']:+.4f} USDT"


async def _manage_open_positions(bot, policy, positions, prices, analyses):
    """Стоп и трейлинг по живой цене каждые 5 минут."""
    for pair in list(positions):
        if pair not in TRADE_PAIRS:
            continue
        price = prices.get(pair)
        analysis = analyses.get(pair)
        if price is None or analysis is None:
            continue
        pos = positions[pair]
        entry = float(pos.get("entry_price"))
        entry_atr = float(pos.get("entry_atr") or await get_trade_state("entry_atr", pair) or analysis["atr"])
        highest = float(await get_trade_state("highest_price", pair) or entry)
        highest = max(highest, price)
        await set_trade_state("highest_price", highest, pair)
        lv = st.stop_level(entry, entry_atr, highest, analysis["atr"], policy)
        await set_trade_state("current_stop", lv["stop"], pair)
        await set_trade_state("stop_stage", lv["stage"], pair)
        if price <= lv["stop"]:
            reason = st.exit_reason_for(lv["stage"])
            logger.info("[%s] Exit %s: price %.4f <= stop %.4f", pair, reason, price, lv["stop"])
            result = await trade_engine.close_long(pair, price, reason)
            if result.get("success"):
                policy = await _after_close(bot, policy, pair, result, reason)
    return policy


async def _trade_job_impl(context: ContextTypes.DEFAULT_TYPE):
    policy = await load_policy()
    silence_days = silence_days_from(await get_last_trade_at())
    policy, esc_msg = await maybe_escalate_silence(policy, silence_days)
    if esc_msg:
        await _notify(context.bot, esc_msg)

    positions = await _reconcile()
    avg_sentiment = _avg_sentiment(await get_recent_sentiments(12))

    analyses = {}
    for pair in TRADE_PAIRS:
        a = await trade_engine.analyze_pair(pair, sentiment=avg_sentiment, policy=policy)
        if a:
            analyses[pair] = a
            await set_trade_state("last_trade_signal", a["signal"], pair)
            await set_trade_state("last_checks", a["checks"], pair)
            await set_trade_state("last_snapshot", a["snapshot"], pair)
    prices = await fetch_prices(TRADE_PAIRS)
    for pair, a in analyses.items():
        prices.setdefault(pair, a["last_price"])

    policy = await _manage_open_positions(context.bot, policy, positions, prices, analyses)

    paused, pause_reason = await trading_pause_status()
    wallet = await get_wallet()
    positions = wallet.get("positions") or {}
    daily = await get_daily_trades(24)
    buys_today = len([t for t in daily if t[1] == "BUY"])
    pnl_today = sum(float(t[4] or 0) for t in daily if t[1] == "SELL")
    daily_loss = await _daily_loss_hit(float(wallet.get("initial_deposit_usdt", 20.0)), pnl_today)

    candidates = []
    for pair, a in analyses.items():
        last_eval = await get_trade_state("last_eval_candle", pair)
        new_candle = last_eval != a["candle_ts"]
        if new_candle:
            await set_trade_state("last_eval_candle", a["candle_ts"], pair)
        logger.info("[%s] %s | %s | htf=%s | new_candle=%s",
                    pair, a["signal"], "; ".join(a["reasons"]), a["htf_trend"], new_candle)
        if a["signal"] != "BUY" or not new_candle or pair in positions:
            continue
        skip = None
        if paused:
            skip = pause_reason
        elif daily_loss:
            skip = f"дневной лимит убытка {MAX_DAILY_LOSS_PCT:g}%"
        elif buys_today >= int(policy["max_daily_buys"]):
            skip = f"лимит BUY/день ({policy['max_daily_buys']})"
        elif _in_cooldown(await get_trade_state("last_exit_at", pair)):
            skip = "кулдаун после выхода"
        if skip:
            logger.info("[%s] BUY пропущен: %s", pair, skip)
            await set_trade_state("last_trade_decision", f"HOLD ({skip})", pair)
            continue
        candidates.append(pair)

    if not candidates:
        return

    stats = {
        "buys_today": buys_today,
        "pnl_today_usdt": round(pnl_today, 4),
        "open_positions": list(positions),
        "recent_lessons": await load_trade_lessons(5),
    }
    snaps = [analyses[p]["snapshot"] for p in candidates]
    cycle = await evaluate_cycle(snaps, policy, stats)
    policy, patch_msg = await apply_policy_patch(policy, cycle.get("policy_patch"))
    if patch_msg:
        await _notify(context.bot, patch_msg)
    decisions = {d["pair"]: d for d in cycle.get("decisions") or []}
    min_conf = float(policy["min_confidence"])

    for pair in candidates:
        a = analyses[pair]
        d = decisions.get(pair) or {"action": "BUY", "confidence": min_conf, "reason": "нет ответа модели"}
        await set_trade_state("last_gemini_action", d["action"], pair)
        await set_trade_state("last_gemini_confidence", d["confidence"], pair)
        await set_trade_state("last_gemini_reason", d["reason"], pair)
        if cycle.get("ok") and (d["action"] != "BUY" or d["confidence"] < min_conf):
            await set_trade_state("last_trade_decision", "HOLD (вето модели)", pair)
            logger.info("[%s] Model veto: %s %.2f — %s", pair, d["action"], d["confidence"], d["reason"])
            continue
        if buys_today >= int(policy["max_daily_buys"]):
            break
        price = prices.get(pair) or a["last_price"]
        result = await trade_engine.open_long(pair, price, a["atr"], policy, avg_sentiment)
        if not result.get("success"):
            await set_trade_state("last_trade_decision", f"HOLD ({result.get('reason')})", pair)
            logger.info("[%s] BUY не исполнен: %s", pair, result.get("reason"))
            continue
        buys_today += 1
        await set_trade_state("last_trade_decision", "BUY", pair)
        summary = await get_wallet_summary(prices)
        rate = summary["usdt_kzt_rate"]
        sizing = result.get("sizing") or {}
        risk_note = (f"риск {format_money(sizing.get('risk_usdt', 0), rate)}"
                     + (" (ограничен слотом)" if sizing.get("limited_by") == "slot" else ""))
        model_line = (f"Модель: <code>{d['action']}</code> ({d['confidence']:.2f}) — {html.escape(str(d['reason']))}"
                      if cycle.get("ok") else f"Модель недоступна, вход по техсигналу")
        await _notify(context.bot, (
            f"🚀 <b>ПОКУПКА: {pair}</b>\n\n"
            f"Цена входа: <code>{result['price']:.4f}</code>\n"
            f"Объём: <code>{result['qty']}</code> | сумма {format_money(result['notional'], rate, code=True)}\n"
            f"Стоп: <code>{result['stop']:.4f}</code> ({(result['stop'] / result['price'] - 1) * 100:.2f}%), {risk_note}\n"
            f"Сигнал: пробой {policy['breakout_bars']} свечей 1h, тренд 4h ↑, ATR {a['snapshot']['atr_pct']}%\n"
            f"{model_line}\n"
            f"💼 Свободно: {format_money(summary['cash_usdt'], rate, code=True)} | "
            f"Слоты: <code>{summary['positions_count']}/{summary['max_positions']}</code>\n"
            f"Режим: {'🧪 PAPER' if PAPER_MODE else '💰 LIVE'}"
        ))


async def send_daily_trade_analytics(update: Update | ContextTypes.DEFAULT_TYPE, context: ContextTypes.DEFAULT_TYPE = None):
    if not hasattr(update, "update_id"):
        context = update
        update = None
    policy = await load_policy()
    silence_days = silence_days_from(await get_last_trade_at())
    rows = await get_daily_trades(24)
    prices = await fetch_prices(TRADE_PAIRS)
    wallet_sum = await get_wallet_summary(prices)
    rate = wallet_sum["usdt_kzt_rate"]
    paused, pause_reason = await trading_pause_status()
    wallet_block = (
        f"💼 <b>Кошелек:</b>\n"
        f"Депозит: {format_money(wallet_sum['initial_usdt'], rate, code=True)}\n"
        f"Свободно: {format_money(wallet_sum['cash_usdt'], rate, code=True)}\n"
        f"Оценка (Equity): {format_money(wallet_sum['equity_usdt'], rate, code=True)}\n"
        f"Общий PnL: <code>{format_money(wallet_sum['total_pnl_usdt'], rate, signed=True)} / "
        f"{'+' if wallet_sum['total_pnl_usdt'] >= 0 else ''}{wallet_sum['roi_pct']}%</code>\n"
        f"Комиссии всего: {format_money(wallet_sum['total_fees_usdt'], rate, code=True)}\n"
        f"Слоты: <code>{wallet_sum['positions_count']}/{wallet_sum['max_positions']}</code>"
        + (f"\n⏸ Пауза: <i>{html.escape(pause_reason)}</i>" if paused else "") + "\n"
    )
    sells = [r for r in rows if r[1] == "SELL"]
    total_pnl = sum(float(r[4] or 0) for r in sells)
    fees = sum(float(r[8] or 0) for r in rows if r[1] == "SELL")
    wins = len([r for r in sells if float(r[4] or 0) > 0])
    losses = len([r for r in sells if float(r[4] or 0) < 0])
    header = (
        f"💰 <b>Статистика за 24ч:</b>\n"
        f"PnL (чистый): {format_money(total_pnl, rate, signed=True, code=True)}\n"
        f"Сделок закрыто: <code>{len(sells)}</code> (W:{wins} / L:{losses}), открыто: "
        f"<code>{len([r for r in rows if r[1] == 'BUY'])}</code>\n"
        f"Комиссии: {format_money(fees, rate, code=True)}\n\n"
        f"{wallet_block}\n"
        f"{format_policy(policy, silence_days)}"
    )
    if not rows:
        await _notify(context.bot, header + "\n\n<i>Сделок не было: условия пробоя в тренде 4h не выполнялись.</i>")
        return
    trades_summary = [{
        "pair": r[0], "side": r[1], "price": r[2], "qty": r[3], "pnl": round(float(r[4] or 0), 4),
        "fee": round(float(r[8] or 0), 4), "reason": r[9], "time": r[7],
    } for r in rows]
    ai_report = ensure_valid_html(await generate_daily_analytics(
        trades_summary, policy=policy, silence_days=silence_days, usdt_kzt_rate=rate))
    await _notify(context.bot, header + "\n\n" + ai_report)


async def trade_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        return await _trade_job_impl(context)
    except Exception as e:
        logger.exception("trade_job failed: %s", e)
    finally:
        trade_engine.release_heavy_libs()
