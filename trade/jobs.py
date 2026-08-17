import html
import json
import logging

from telegram import Update
from telegram.ext import ContextTypes

from ai.trading import evaluate_trade, generate_daily_analytics
from config import ADMIN_ID, MAX_DAILY_TRADES, PAPER_MODE, TRADE_PAIRS
from news.repo import get_recent_sentiments
from trade import engine as trade_engine
from trade.repo import (
    get_daily_trades,
    get_open_position,
    get_trade_state,
    set_trade_state,
)

logger = logging.getLogger(__name__)


async def trade_job(context: ContextTypes.DEFAULT_TYPE):
    """Основной цикл трейдинга: для каждой пары OHLCV -> Indicators -> Signal -> Execute."""
    for pair in TRADE_PAIRS:
        logger.info("Starting trade cycle for %s...", pair)

        df = await trade_engine.fetch_ohlcv(pair)
        if df is None:
            logger.error("Failed to fetch OHLCV data for %s", pair)
            continue

        df = trade_engine.calc_indicators(df)

        avg_sentiment = 0
        rows = await get_recent_sentiments(12)
        if rows:
            sentiments = []
            for r in rows:
                try:
                    if r[0]:
                        data = json.loads(r[0])
                        sentiments.append(data.get("sentiment", 0))
                except Exception:
                    continue
            if sentiments:
                avg_sentiment = sum(sentiments) / len(sentiments)

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
                )
            except (TypeError, ValueError) as e:
                logger.warning("Error calculating risk exit for %s: %s", pair, e)

        technical_signal = trade_engine.get_signal(df, sentiment=avg_sentiment)
        if risk_exit_reason:
            technical_signal = "SELL"

        logger.info(
            "[%s] Tech: %s | ADX: %.1f | ATR: %.4f | RiskExit: %s",
            pair, technical_signal, last_adx, last_atr, risk_exit_reason,
        )

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
            "recent_candles": df[["close", "volume", "rsi"]].tail(3).to_dict("records"),
        }

        should_call_ai = (technical_signal != "HOLD") or (risk_exit_reason is not None)

        if not should_call_ai:
            ai_decision = {
                "action": "HOLD",
                "confidence": 0.0,
                "reason": "Технических сигналов нет, ADX низкий — экономим API",
            }
            signal = "HOLD"
        elif risk_exit_reason:
            ai_decision = {
                "action": "SELL",
                "confidence": 1.0,
                "reason": f"Сработал динамический выход: {risk_exit_reason}",
            }
            signal = "SELL"
        else:
            ai_decision = await evaluate_trade(
                pair=pair,
                market_snapshot=market_snapshot,
                technical_signal=technical_signal,
                avg_sentiment=avg_sentiment,
            )

            if technical_signal == "BUY":
                if ai_decision["action"] == "BUY" and ai_decision["confidence"] >= 0.6:
                    signal = "BUY"
                else:
                    signal = "HOLD"
            elif technical_signal == "SELL":
                if ai_decision["action"] == "BUY" and ai_decision["confidence"] >= 0.8:
                    signal = "HOLD"
                else:
                    signal = "SELL"
            else:
                signal = "HOLD"

        await set_trade_state("last_trade_signal", technical_signal, pair)
        await set_trade_state("last_gemini_action", ai_decision["action"], pair)
        await set_trade_state("last_gemini_confidence", ai_decision["confidence"], pair)
        await set_trade_state("last_gemini_reason", ai_decision["reason"], pair)
        await set_trade_state("last_trade_decision", signal, pair)
        await set_trade_state("last_risk_exit_reason", risk_exit_reason, pair)

        logger.info(
            "[%s] Final Decision: %s | Price: %s | local/qwen3.5-coder: %s (%s)",
            pair, signal, last_price, ai_decision["action"], ai_decision["confidence"],
        )

        if signal == "BUY":
            if current_pos == "in_position":
                continue

            daily_trades = await get_daily_trades(24)
            buy_trades_count = len([t for t in daily_trades if t[1] == "BUY"])
            total_daily_pnl = sum([float(t[4]) for t in daily_trades if t[4] is not None])

            if buy_trades_count >= MAX_DAILY_TRADES:
                logger.warning("Daily trade limit reached (%s). Skipping BUY for %s.", MAX_DAILY_TRADES, pair)
                continue

            if total_daily_pnl < -100:
                logger.warning("Daily drawdown limit reached. Skipping BUY for %s.", pair)
                continue

        if signal == "SELL" and (current_pos == "none" or current_pos is None):
            continue

        if signal == "HOLD":
            continue

        trade_result = await trade_engine.execute_trade(signal, last_price, pair, avg_sentiment, atr=last_atr)

        if trade_result and trade_result.get("success"):
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
                f"local/qwen3.5-coder: <code>{ai_decision['action']}</code> ({ai_decision['confidence']:.2f})\n"
                f"Причина: <code>{html.escape(ai_decision['reason'])}</code>{pnl_text}\n"
                f"Режим: {'🧪 PAPER' if PAPER_MODE else '💰 LIVE'}"
            )

            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
            except Exception as e:
                logger.error("Failed to send trade notification for %s: %s", pair, e)


async def send_daily_trade_analytics(update: Update | ContextTypes.DEFAULT_TYPE, context: ContextTypes.DEFAULT_TYPE = None):
    """Собирает сделки за 24 часа и отправляет аналитику от локальной модели."""
    if not hasattr(update, "update_id"):
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
            logger.error("Error sending empty daily analytics: %s", e)
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
        logger.error("Error sending daily analytics: %s", e)
