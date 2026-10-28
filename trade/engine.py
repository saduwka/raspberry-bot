"""Движок трейдера v2: реальные свечи Binance + чистые индикаторы, без ccxt/pandas в процессе бота."""
import gc
import logging

from config import TRADE_HTF, TRADE_TIMEFRAME
from trade import strategy as st
from trade.market import fetch_klines, fetch_price
from trade.repo import save_trade, set_trade_state, get_trade_state, clear_pair_state

logger = logging.getLogger(__name__)


async def analyze_pair(pair, sentiment=0.0, policy=None, timeframe=TRADE_TIMEFRAME, limit=150):
    """Анализ по последней закрытой свече. Возвращает снимок, сигнал и чек-лист условий."""
    from trade.policy import load_policy

    policy = policy or await load_policy()
    ltf_c = await fetch_klines(pair, timeframe, limit=limit)
    htf_c = await fetch_klines(pair, TRADE_HTF, limit=120)
    if not ltf_c or len(ltf_c) < 70 or not htf_c:
        logger.error("analyze_pair: нет данных для %s", pair)
        return None
    ltf = st.compute(ltf_c)
    htf = st.compute_htf(htf_c)
    i = len(ltf_c) - 1
    trend = st.htf_trend_at(htf, len(htf_c) - 1)
    signal, reasons = st.entry_signal(ltf, i, trend, policy, sentiment)
    checks = st.entry_checks(ltf, i, trend, policy, sentiment)
    snap = st.snapshot(pair, ltf, i, trend, policy)
    return {
        "signal": signal,
        "reasons": reasons,
        "checks": checks,
        "snapshot": snap,
        "candle_ts": ltf_c[-1]["ts"],
        "last_price": snap["price"],
        "atr": snap["atr"],
        "rsi": snap["rsi"],
        "adx": snap["adx"],
        "htf_trend": trend,
        "rows": len(ltf_c),
    }


async def live_price(pair, fallback=None):
    price = await fetch_price(pair)
    return price if price else fallback


async def position_state(pair):
    return {
        "current": await get_trade_state("current_position", pair),
        "entry_price": await get_trade_state("entry_price", pair),
        "qty": await get_trade_state("position_qty", pair),
        "highest": await get_trade_state("highest_price", pair),
        "entry_atr": await get_trade_state("entry_atr", pair),
    }


async def open_long(pair, price, atr, policy, sentiment=0.0, reason="BREAKOUT"):
    from trade.wallet import allocate_buy, calculate_buy_qty, can_open_position

    can_buy, why = await can_open_position(pair)
    if not can_buy:
        return {"success": False, "reason": why}
    stop_price = price - float(atr) * float(policy.get("atr_stop", 2.0))
    qty, notional, info = await calculate_buy_qty(pair, price, stop_price, float(policy.get("risk_pct", 1.0)))
    if qty <= 0:
        return {"success": False, "reason": "Размер ордера ниже минимального порога"}
    pos = await allocate_buy(pair, price, qty, extra={"entry_atr": float(atr), "stop_initial": stop_price})
    await save_trade(pair=pair, side="BUY", price=pos["entry_price"], qty=qty, pnl=0.0,
                     signal=f"V2_{reason}", sentiment=str(sentiment), fee=pos["fee_buy"],
                     entry_price=pos["entry_price"], reason=reason)
    await set_trade_state("current_position", "in_position", pair)
    await set_trade_state("entry_price", pos["entry_price"], pair)
    await set_trade_state("position_qty", qty, pair)
    await set_trade_state("highest_price", pos["entry_price"], pair)
    await set_trade_state("entry_atr", float(atr), pair)
    return {"success": True, "price": pos["entry_price"], "qty": qty, "notional": notional,
            "fee": pos["fee_buy"], "stop": stop_price, "sizing": info}


async def close_long(pair, price, reason, sentiment=0.0):
    from trade.wallet import release_sell

    state = await position_state(pair)
    try:
        qty = float(state["qty"]) if state["qty"] is not None else None
    except (TypeError, ValueError):
        qty = None
    result = await release_sell(pair, price, qty)
    if not result.get("qty"):
        await clear_pair_state([pair])
        return {"success": False, "reason": "Позиция не найдена"}
    await save_trade(pair=pair, side="SELL", price=result["exit_price"], qty=result["qty"], pnl=result["pnl"],
                     signal=f"V2_{reason}", sentiment=str(sentiment), fee=result["fee_total"],
                     entry_price=result["entry_price"], reason=reason)
    await clear_pair_state([pair])
    await set_trade_state("current_position", "none", pair)
    return {"success": True, **result}


async def execute_trade(signal, price, pair, sentiment_score=0, atr=None, policy=None):
    """Совместимость со старым API."""
    from trade.policy import load_policy

    policy = policy or await load_policy()
    if signal == "BUY":
        return await open_long(pair, price, atr, policy, sentiment_score)
    if signal == "SELL":
        return await close_long(pair, price, "MANUAL", sentiment_score)
    return None


def release_heavy_libs():
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass
