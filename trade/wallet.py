import logging
from datetime import datetime, timezone

from config import (
    TRADE_FEE_PCT,
    TRADE_SLIPPAGE_PCT,
    USDT_KZT_RATE,
    WALLET_INITIAL_KZT,
    WALLET_MAX_POSITIONS,
    WALLET_MIN_ORDER_USDT,
)
from trade.repo import get_trade_state, set_trade_state

logger = logging.getLogger(__name__)

WALLET_KEY = "virtual_wallet"
WALLET_PAIR = "WALLET"
FEE = TRADE_FEE_PCT / 100.0
SLIP = TRADE_SLIPPAGE_PCT / 100.0


def format_money(usdt: float, rate: float | None = None, *, signed: bool = False, code: bool = False) -> str:
    """Формат суммы: «12.34 USDT (6 170 ₸)» или со знаком для PnL."""
    rate = float(rate if rate is not None else (USDT_KZT_RATE or 500.0))
    usdt = float(usdt or 0.0)
    kzt = round(usdt * rate, 0)
    if signed:
        text = f"{'+' if usdt > 0 else ''}{usdt:.2f} USDT ({'+' if kzt > 0 else ''}{kzt:,.0f} ₸)"
    else:
        text = f"{usdt:.2f} USDT ({kzt:,.0f} ₸)"
    return f"<code>{text}</code>" if code else text


def _round_qty(qty: float, price: float) -> float:
    """Округление объёма вниз с учётом цены инструмента."""
    import math
    digits = 6 if price >= 10000 else 5 if price >= 1000 else 3 if price >= 50 else 2 if price >= 1 else 1
    factor = 10 ** digits
    return math.floor(qty * factor) / factor


def _new_wallet(deposit_kzt: float, rate: float) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    deposit_usdt = round(deposit_kzt / rate, 2)
    return {
        "initial_deposit_kzt": deposit_kzt,
        "usdt_kzt_rate": rate,
        "initial_deposit_usdt": deposit_usdt,
        "cash_usdt": deposit_usdt,
        "positions": {},
        "realized_pnl_usdt": 0.0,
        "total_fees_usdt": 0.0,
        "total_trades_count": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "created_at": now_iso,
        "updated_at": now_iso,
    }


async def get_wallet() -> dict:
    data = await get_trade_state(WALLET_KEY, pair=WALLET_PAIR)
    if isinstance(data, dict) and "cash_usdt" in data:
        data.setdefault("initial_deposit_kzt", WALLET_INITIAL_KZT)
        data.setdefault("usdt_kzt_rate", USDT_KZT_RATE)
        data.setdefault("initial_deposit_usdt", data["initial_deposit_kzt"] / data["usdt_kzt_rate"])
        data.setdefault("positions", {})
        data.setdefault("realized_pnl_usdt", 0.0)
        data.setdefault("total_fees_usdt", 0.0)
        data.setdefault("total_trades_count", 0)
        data.setdefault("winning_trades", 0)
        data.setdefault("losing_trades", 0)
        return data
    rate = USDT_KZT_RATE if USDT_KZT_RATE > 0 else 500.0
    wallet = _new_wallet(WALLET_INITIAL_KZT if WALLET_INITIAL_KZT > 0 else 10000.0, rate)
    await save_wallet(wallet)
    logger.info("Initialized new trading wallet: %s USDT", wallet["initial_deposit_usdt"])
    return wallet


async def save_wallet(wallet: dict) -> None:
    wallet["updated_at"] = datetime.now(timezone.utc).isoformat()
    await set_trade_state(WALLET_KEY, wallet, pair=WALLET_PAIR)


async def reset_wallet(deposit_kzt: float = None, rate: float = None) -> dict:
    rate = rate or USDT_KZT_RATE or 500.0
    wallet = _new_wallet(deposit_kzt or WALLET_INITIAL_KZT or 10000.0, rate)
    await save_wallet(wallet)
    logger.info("Reset trading wallet to %s USDT", wallet["initial_deposit_usdt"])
    return wallet


def wallet_equity(wallet: dict, prices: dict | None = None) -> float:
    prices = prices or {}
    eq = float(wallet.get("cash_usdt", 0.0))
    for pair, pos in (wallet.get("positions") or {}).items():
        qty = float(pos.get("qty", 0.0))
        eq += qty * float(prices.get(pair) or pos.get("entry_price") or 0.0)
    return eq


async def can_open_position(pair: str) -> tuple[bool, str]:
    wallet = await get_wallet()
    positions = wallet.get("positions", {})
    if pair in positions:
        return False, f"Позиция {pair} уже открыта"
    if len(positions) >= WALLET_MAX_POSITIONS:
        return False, f"Все слоты заняты ({len(positions)}/{WALLET_MAX_POSITIONS})"
    cash = float(wallet.get("cash_usdt", 0.0))
    if cash < WALLET_MIN_ORDER_USDT * (1 + FEE):
        rate = float(wallet.get("usdt_kzt_rate", USDT_KZT_RATE or 500.0))
        return False, f"Недостаточно средств: свободно {format_money(cash, rate)}"
    return True, "OK"


async def calculate_buy_qty(pair: str, price: float, stop_price: float | None = None,
                            risk_pct: float = 1.0) -> tuple[float, float, dict]:
    """Размер позиции от риска: (капитал × risk%) / (расстояние до стопа + издержки).

    Ограничен бюджетом свободного слота и наличными. Возвращает (qty, notional, info).
    """
    info = {"risk_usdt": 0.0, "slot_budget": 0.0, "limited_by": None}
    if price <= 0:
        return 0.0, 0.0, info
    wallet = await get_wallet()
    positions = wallet.get("positions", {})
    cash = float(wallet.get("cash_usdt", 0.0))
    remaining = max(1, WALLET_MAX_POSITIONS - len(positions))
    slot_budget = cash / remaining
    budget = slot_budget
    info["slot_budget"] = round(slot_budget, 4)
    info["limited_by"] = "slot"
    if stop_price and 0 < stop_price < price:
        equity = wallet_equity(wallet)
        risk_usdt = equity * float(risk_pct) / 100.0
        per_unit_loss = (price - stop_price) / price + 2 * FEE + 2 * SLIP
        risk_budget = risk_usdt / per_unit_loss
        info["risk_usdt"] = round(risk_usdt, 4)
        if risk_budget < budget:
            budget = risk_budget
            info["limited_by"] = "risk"
    budget = max(budget, WALLET_MIN_ORDER_USDT) if slot_budget >= WALLET_MIN_ORDER_USDT else 0.0
    budget = min(budget, cash / (1 + FEE + SLIP))
    fill = price * (1 + SLIP)
    qty = _round_qty(budget / fill, price)
    notional = round(qty * fill, 4)
    if notional < WALLET_MIN_ORDER_USDT or qty <= 0:
        return 0.0, 0.0, info
    return qty, notional, info


async def allocate_buy(pair: str, price: float, qty: float, extra: dict | None = None) -> dict:
    """Покупка: цена с проскальзыванием, комиссия списывается сверху."""
    wallet = await get_wallet()
    positions = wallet.setdefault("positions", {})
    cash = float(wallet.get("cash_usdt", 0.0))
    fill = round(price * (1 + SLIP), 8)
    notional = round(qty * fill, 6)
    fee = round(notional * FEE, 6)
    if notional + fee > cash:
        logger.warning("Cost %s exceeds cash %s for %s, capping", notional + fee, cash, pair)
        notional = max(0.0, cash - fee)
    wallet["cash_usdt"] = round(cash - notional - fee, 6)
    wallet["total_fees_usdt"] = round(float(wallet.get("total_fees_usdt", 0.0)) + fee, 6)
    positions[pair] = {
        "pair": pair,
        "qty": qty,
        "entry_price": fill,
        "signal_price": price,
        "cost_usdt": notional,
        "fee_buy": fee,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        **(extra or {}),
    }
    await save_wallet(wallet)
    logger.info("Wallet BUY: %s qty=%s fill=%s cost=%s fee=%s cash=%s",
                pair, qty, fill, notional, fee, wallet["cash_usdt"])
    return positions[pair]


async def release_sell(pair: str, price: float, qty: float | None = None) -> dict:
    """Продажа: цена с проскальзыванием, PnL чистый (обе комиссии учтены)."""
    wallet = await get_wallet()
    positions = wallet.setdefault("positions", {})
    pos = positions.pop(pair, None) or {}
    qty = float(qty if qty is not None else pos.get("qty", 0.0))
    entry_price = float(pos.get("entry_price", price))
    entry_cost = float(pos.get("cost_usdt", qty * entry_price))
    fee_buy = float(pos.get("fee_buy", 0.0))
    fill = round(price * (1 - SLIP), 8)
    proceeds = round(qty * fill, 6)
    fee_sell = round(proceeds * FEE, 6)
    pnl = round(proceeds - fee_sell - entry_cost - fee_buy, 6)

    wallet["cash_usdt"] = round(float(wallet.get("cash_usdt", 0.0)) + proceeds - fee_sell, 6)
    wallet["realized_pnl_usdt"] = round(float(wallet.get("realized_pnl_usdt", 0.0)) + pnl, 6)
    wallet["total_fees_usdt"] = round(float(wallet.get("total_fees_usdt", 0.0)) + fee_sell, 6)
    wallet["total_trades_count"] = int(wallet.get("total_trades_count", 0)) + 1
    if pnl > 0:
        wallet["winning_trades"] = int(wallet.get("winning_trades", 0)) + 1
    elif pnl < 0:
        wallet["losing_trades"] = int(wallet.get("losing_trades", 0)) + 1
    await save_wallet(wallet)
    logger.info("Wallet SELL: %s qty=%s fill=%s proceeds=%s fee=%s pnl=%s cash=%s",
                pair, qty, fill, proceeds, fee_sell, pnl, wallet["cash_usdt"])
    return {
        "pnl": pnl,
        "proceeds": proceeds,
        "entry_cost": entry_cost,
        "entry_price": entry_price,
        "exit_price": fill,
        "fee_total": round(fee_buy + fee_sell, 6),
        "qty": qty,
        "opened_at": pos.get("opened_at"),
        "cash_usdt": wallet["cash_usdt"],
    }


async def get_wallet_summary(current_prices: dict = None) -> dict:
    wallet = await get_wallet()
    rate = float(wallet.get("usdt_kzt_rate", USDT_KZT_RATE or 500.0))
    initial_kzt = float(wallet.get("initial_deposit_kzt", WALLET_INITIAL_KZT or 10000.0))
    initial_usdt = float(wallet.get("initial_deposit_usdt", initial_kzt / rate))
    cash_usdt = float(wallet.get("cash_usdt", 0.0))
    positions = wallet.get("positions", {})
    current_prices = current_prices or {}

    pos_details = []
    cost_sum = market_sum = 0.0
    for pair, pos in positions.items():
        qty = float(pos.get("qty", 0.0))
        entry_price = float(pos.get("entry_price", 0.0))
        cost = float(pos.get("cost_usdt", qty * entry_price))
        cur_price = float(current_prices.get(pair) or entry_price)
        market = round(qty * cur_price, 4)
        unrealized = round(market * (1 - FEE) - cost - float(pos.get("fee_buy", 0.0)), 4)
        cost_sum += cost
        market_sum += market
        pos_details.append({
            "pair": pair,
            "qty": qty,
            "entry_price": entry_price,
            "cur_price": cur_price,
            "cost_usdt": cost,
            "market_usdt": market,
            "unrealized_pnl": unrealized,
            "unrealized_pct": round(unrealized / cost * 100, 2) if cost > 0 else 0.0,
            "opened_at": pos.get("opened_at"),
        })

    equity_usdt = round(cash_usdt + market_sum, 4)
    total_pnl_usdt = round(equity_usdt - initial_usdt, 4)
    return {
        "initial_kzt": initial_kzt,
        "initial_usdt": initial_usdt,
        "usdt_kzt_rate": rate,
        "cash_usdt": cash_usdt,
        "cash_kzt": round(cash_usdt * rate, 2),
        "positions_count": len(positions),
        "max_positions": WALLET_MAX_POSITIONS,
        "positions": pos_details,
        "positions_cost_usdt": round(cost_sum, 2),
        "positions_market_usdt": round(market_sum, 2),
        "equity_usdt": equity_usdt,
        "equity_kzt": round(equity_usdt * rate, 2),
        "total_pnl_usdt": total_pnl_usdt,
        "total_pnl_kzt": round(total_pnl_usdt * rate, 2),
        "roi_pct": round(total_pnl_usdt / initial_usdt * 100, 2) if initial_usdt > 0 else 0.0,
        "realized_pnl_usdt": float(wallet.get("realized_pnl_usdt", 0.0)),
        "total_fees_usdt": float(wallet.get("total_fees_usdt", 0.0)),
        "total_trades_count": int(wallet.get("total_trades_count", 0)),
        "winning_trades": int(wallet.get("winning_trades", 0)),
        "losing_trades": int(wallet.get("losing_trades", 0)),
        "created_at": wallet.get("created_at"),
    }


def format_wallet_message(summary: dict) -> str:
    rate = summary["usdt_kzt_rate"]
    pnl_emoji = "🟢" if summary["total_pnl_usdt"] >= 0 else "🔴"
    if summary["positions"]:
        lines = []
        for p in summary["positions"]:
            sign = "+" if p["unrealized_pnl"] >= 0 else ""
            lines.append(
                f"  • <b>{p['pair']}</b>: {p['qty']} (вход <code>{p['entry_price']:.4f}</code>) "
                f"→ {format_money(p['market_usdt'], rate, code=True)} "
                f"({format_money(p['unrealized_pnl'], rate, signed=True)} / {sign}{p['unrealized_pct']:.1f}%)"
            )
        pos_block = "\n".join(lines)
    else:
        pos_block = "  <i>Все слоты свободны</i>"
    return (
        f"💼 <b>Торговый кошелек (PAPER)</b>\n\n"
        f"💰 <b>Начальный депозит:</b> {format_money(summary['initial_usdt'], rate, code=True)}\n"
        f"💵 <b>Свободно:</b> {format_money(summary['cash_usdt'], rate, code=True)}\n"
        f"📊 <b>В позициях:</b> {format_money(summary['positions_market_usdt'], rate, code=True)}\n"
        f"📈 <b>Оценка (Equity):</b> {format_money(summary['equity_usdt'], rate, code=True)}\n\n"
        f"{pnl_emoji} <b>Общий PnL:</b> <b>{format_money(summary['total_pnl_usdt'], rate, signed=True)} / "
        f"{'+' if summary['total_pnl_usdt'] >= 0 else ''}{summary['roi_pct']}%</b>\n"
        f"🧾 <b>Комиссии:</b> {format_money(summary['total_fees_usdt'], rate, code=True)}\n"
        f"🎰 <b>Слоты:</b> <code>{summary['positions_count']} / {summary['max_positions']}</code>\n"
        f"🏆 <b>Сделок закрыто:</b> {summary['total_trades_count']} "
        f"(W: {summary['winning_trades']} / L: {summary['losing_trades']})\n\n"
        f"<b>Открытые позиции:</b>\n{pos_block}\n\n"
        f"<i>Курс: 1 USDT = {rate:.0f} ₸ | комиссия {TRADE_FEE_PCT}% + проскальзывание "
        f"{TRADE_SLIPPAGE_PCT}% на сторону</i>"
    )
