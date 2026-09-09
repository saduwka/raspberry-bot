import html
import json
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from ai.trading import evaluate_cycle, generate_daily_analytics
from config import (
    ADMIN_ID,
    ENTRY_COOLDOWN_SECONDS,
    MAX_DAILY_LOSS_USDT,
    PAPER_MODE,
    TRADE_PAIRS,
)
from news.repo import get_recent_sentiments
from trade import engine as trade_engine
from trade.policy import (
    apply_policy_patch,
    format_policy,
    load_policy,
    maybe_boot_silence,
    maybe_escalate_silence,
    silence_days_from,
)
from trade.repo import (
    get_daily_trades,
    get_last_trade_at,
    get_open_position,
    get_trade_state,
    set_trade_state,
)

logger = logging.getLogger(__name__)


def _avg_sentiment(rows) -> float:
    if not rows:
        return 0.0
    sentiments = []
    for r in rows:
        try:
            if r[0]:
                data = json.loads(r[0])
                sentiments.append(data.get("sentiment", 0))
        except Exception:
            continue
    if not sentiments:
        return 0.0
    return sum(sentiments) / len(sentiments)


def _in_cooldown(last_entry_at) -> bool:
    if not last_entry_at:
        return False
    try:
        text = str(last_entry_at).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - dt).total_seconds()
        return elapsed < ENTRY_COOLDOWN_SECONDS
    except (TypeError, ValueError):
        return False


async def _notify(bot, text: str):
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
    except Exception as e:
        logger.error("Failed to send trade message: %s", e)


async def trade_job(context: ContextTypes.DEFAULT_TYPE):
    """Цикл: политика -> снимки всех пар -> один LLM -> caps -> execute."""
    policy = await load_policy()
    last_trade_at = await get_last_trade_at()
    silence_days = silence_days_from(last_trade_at)

    policy, boot_msg = await maybe_boot_silence(policy, silence_days)
    if boot_msg:
        await _notify(context.bot, boot_msg)
    else:
        policy, esc_msg = await maybe_escalate_silence(policy, silence_days)
        if esc_msg:
            await _notify(context.bot, esc_msg)

    rows = await get_recent_sentiments(12)
    avg_sentiment = _avg_sentiment(rows)
    daily_trades = await get_daily_trades(24)
    buy_trades_count = len([t for t in daily_trades if t[1] == "BUY"])
    total_daily_pnl = sum(float(t[4]) for t in daily_trades if t[4] is not None)

    snapshots = []
    pair_ctx = {}

    for pair in TRADE_PAIRS:
        logger.info("Starting trade cycle for %s...", pair)
        df = await trade_engine.fetch_ohlcv(pair)
        if df is None or df.empty:
            logger.error("Failed to fetch OHLCV data for %s", pair)
            continue

        df = trade_engine.calc_indicators(df)
        if df is None or df.empty:
            logger.warning("No data for indicators for %s, skipping", pair)
            continue

        last_row = df.iloc[-1]
        last_price = last_row["close"]
        last_atr = last_row["atr"]
        last_adx = last_row["adx"]

        current_pos = await get_open_position(pair)
        entry_price = await get_trade_state("entry_price", pair)
        highest_price = await get_trade_state("highest_price", pair)

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
                    highest_price=float(highest_price) if highest_price else None,
                    policy=policy,
                )
            except (TypeError, ValueError) as e:
                logger.warning("Error calculating risk exit for %s: %s", pair, e)

        technical_signal = trade_engine.get_signal(df, sentiment=avg_sentiment, policy=policy)
        if risk_exit_reason:
            technical_signal = "SELL"

        logger.info(
            "[%s] Tech hint: %s | ADX: %.1f | ATR: %.4f | RiskExit: %s | agg=%s",
            pair, technical_signal, last_adx, last_atr, risk_exit_reason, policy["aggression"],
        )

        snap = {
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
        }
        snapshots.append(snap)
        pair_ctx[pair] = {
            "snap": snap,
            "last_price": last_price,
            "last_atr": last_atr,
            "current_pos": current_pos,
            "risk_exit_reason": risk_exit_reason,
            "technical_signal": technical_signal,
        }

    if not snapshots:
        return

    stats = {
        "days_silent": round(silence_days, 1) if silence_days is not None else None,
        "buys_today": buy_trades_count,
        "pnl_today": round(total_daily_pnl, 2),
        "max_daily_loss_usdt": MAX_DAILY_LOSS_USDT,
    }
    cycle = await evaluate_cycle(snapshots, policy, stats)
    logger.info(
        "Trade cycle AI done ok=%s provider=local pairs=%s patch=%s",
        cycle.get("ok"),
        [d.get("pair") for d in cycle.get("decisions") or []],
        bool(cycle.get("policy_patch")),
    )
    policy, patch_msg = await apply_policy_patch(policy, cycle.get("policy_patch"))
    if patch_msg:
        await _notify(context.bot, patch_msg)

    decisions = {d["pair"]: d for d in cycle.get("decisions") or []}

    for pair, ctx in pair_ctx.items():
        ai_decision = decisions.get(pair) or {
            "action": "HOLD",
            "confidence": 0.0,
            "reason": "нет решения",
            "risk_usdt": policy["risk_usdt"],
        }
        technical_signal = ctx["technical_signal"]
        risk_exit_reason = ctx["risk_exit_reason"]
        current_pos = ctx["current_pos"]
        last_price = ctx["last_price"]
        last_atr = ctx["last_atr"]

        if risk_exit_reason:
            signal = "SELL"
            ai_decision = {
                "action": "SELL",
                "confidence": 1.0,
                "reason": f"Сработал динамический выход: {risk_exit_reason}",
                "risk_usdt": policy["risk_usdt"],
            }
        else:
            signal = ai_decision["action"]
            min_conf = float(policy["min_confidence"])
            if signal == "BUY" and ai_decision["confidence"] < min_conf:
                signal = "HOLD"
                ai_decision["reason"] = (
                    f"Низкая уверенность ({ai_decision['confidence']:.2f} < {min_conf}): "
                    f"{ai_decision.get('reason', '')}"
                )
            if signal == "BUY" and cycle.get("ok") is False and technical_signal != "BUY":
                signal = "HOLD"

        await set_trade_state("last_trade_signal", technical_signal, pair)
        await set_trade_state("last_gemini_action", ai_decision["action"], pair)
        await set_trade_state("last_gemini_confidence", ai_decision["confidence"], pair)
        await set_trade_state("last_gemini_reason", ai_decision["reason"], pair)
        await set_trade_state("last_trade_decision", signal, pair)
        await set_trade_state("last_risk_exit_reason", risk_exit_reason, pair)

        logger.info(
            "[%s] Final Decision: %s | Price: %s | AI: %s (%s) | agg=%s",
            pair, signal, last_price, ai_decision["action"], ai_decision["confidence"], policy["aggression"],
        )

        if signal == "BUY":
            if current_pos == "in_position":
                continue
            if _in_cooldown(await get_trade_state("last_entry_at", pair)):
                logger.info("Cooldown active, skip BUY for %s", pair)
                continue
            if buy_trades_count >= int(policy["max_daily_buys"]):
                logger.warning("Daily buy limit reached (%s). Skipping BUY for %s.",
                               policy["max_daily_buys"], pair)
                continue
            if total_daily_pnl < -MAX_DAILY_LOSS_USDT:
                logger.warning("Daily drawdown limit reached. Skipping BUY for %s.", pair)
                continue

        if signal == "SELL" and (current_pos == "none" or current_pos is None):
            continue

        if signal == "HOLD":
            continue

        pair_policy = dict(policy)
        if ai_decision.get("risk_usdt") is not None:
            try:
                pair_policy["risk_usdt"] = max(5.0, min(20.0, float(ai_decision["risk_usdt"])))
            except (TypeError, ValueError):
                pass

        trade_result = await trade_engine.execute_trade(
            signal, last_price, pair, avg_sentiment, atr=last_atr, policy=pair_policy,
        )

        if trade_result and trade_result.get("success"):
            if signal == "BUY":
                buy_trades_count += 1
                await set_trade_state("last_entry_at", datetime.now(timezone.utc).isoformat(), pair)
            if signal == "SELL":
                await set_trade_state("highest_price", None, pair)
                pnl = trade_result.get("pnl", 0.0) or 0.0
                total_daily_pnl += float(pnl)

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
                f"Агрессия: <code>{policy['aggression']}/5</code>\n"
                f"local/qwen3.5-coder: <code>{ai_decision['action']}</code> ({ai_decision['confidence']:.2f})\n"
                f"Причина: <code>{html.escape(str(ai_decision['reason']))}</code>{pnl_text}\n"
                f"Режим: {'🧪 PAPER' if PAPER_MODE else '💰 LIVE'}"
            )
            await _notify(context.bot, text)


async def send_daily_trade_analytics(update: Update | ContextTypes.DEFAULT_TYPE, context: ContextTypes.DEFAULT_TYPE = None):
    """Сделки за 24 часа + политика + аналитика модели."""
    if not hasattr(update, "update_id"):
        context = update
        update = None

    logger.info("Generating daily trade analytics...")
    policy = await load_policy()
    last_trade_at = await get_last_trade_at()
    silence_days = silence_days_from(last_trade_at)
    policy, esc_msg = await maybe_escalate_silence(policy, silence_days)
    if esc_msg:
        await _notify(context.bot, esc_msg)

    rows = await get_daily_trades(24)
    policy_block = format_policy(policy, silence_days)

    if not rows:
        logger.info("No trades today, sending empty status report.")
        header = (
            f"💰 <b>Статистика за 24ч:</b>\n"
            f"PnL: <code>0.00 USDT</code>\n"
            f"Сделок: <code>0</code>\n"
            f"Статус: <i>Активен, сделок не было.</i>\n\n"
            f"{policy_block}"
        )
        await _notify(context.bot, header)
        return

    trades_summary = []
    total_pnl = 0.0
    wins = 0
    losses = 0

    for r in rows:
        pnl = float(r[4]) if r[4] is not None else 0.0
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        trades_summary.append({
            "pair": r[0],
            "side": r[1],
            "price": r[2],
            "qty": r[3],
            "pnl": round(pnl, 2),
            "signal": r[5],
            "sentiment": r[6],
            "time": r[7],
        })

    winrate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    ai_report = await generate_daily_analytics(trades_summary, policy=policy, silence_days=silence_days)

    header = (
        f"💰 <b>Статистика за 24ч:</b>\n"
        f"PnL: <code>{total_pnl:.2f} USDT</code>\n"
        f"Сделок: <code>{len(trades_summary)}</code> (W:{wins} / L:{losses})\n"
        f"Winrate: <code>{winrate:.1f}%</code>\n\n"
        f"{policy_block}\n\n"
    )
    await _notify(context.bot, header + ai_report)
