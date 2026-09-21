import logging
from datetime import datetime, timezone

from config import (
    USDT_KZT_RATE,
    WALLET_INITIAL_KZT,
    WALLET_MAX_POSITIONS,
    WALLET_MIN_ORDER_USDT,
)
from trade.repo import get_trade_state, set_trade_state

logger = logging.getLogger(__name__)

WALLET_KEY = "virtual_wallet"
WALLET_PAIR = "WALLET"


def _round_qty(qty: float, price: float) -> float:
    """Округление объема с учетом стоимости инструмента."""
    if price >= 10000:
        return round(qty, 6)
    elif price >= 1000:
        return round(qty, 5)
    elif price >= 50:
        return round(qty, 3)
    elif price >= 1:
        return round(qty, 2)
    return round(qty, 1)


async def get_wallet() -> dict:
    """Загружает состояние кошелька или инициализирует его при первом обращении."""
    data = await get_trade_state(WALLET_KEY, pair=WALLET_PAIR)
    if isinstance(data, dict) and "cash_usdt" in data:
        data.setdefault("initial_deposit_kzt", WALLET_INITIAL_KZT)
        data.setdefault("usdt_kzt_rate", USDT_KZT_RATE)
        data.setdefault("initial_deposit_usdt", data["initial_deposit_kzt"] / data["usdt_kzt_rate"])
        data.setdefault("positions", {})
        data.setdefault("realized_pnl_usdt", 0.0)
        data.setdefault("total_trades_count", 0)
        data.setdefault("winning_trades", 0)
        data.setdefault("losing_trades", 0)
        return data

    rate = USDT_KZT_RATE if USDT_KZT_RATE > 0 else 500.0
    initial_kzt = WALLET_INITIAL_KZT if WALLET_INITIAL_KZT > 0 else 10000.0
    initial_usdt = round(initial_kzt / rate, 2)
    now_iso = datetime.now(timezone.utc).isoformat()

    new_wallet = {
        "initial_deposit_kzt": initial_kzt,
        "usdt_kzt_rate": rate,
        "initial_deposit_usdt": initial_usdt,
        "cash_usdt": initial_usdt,
        "positions": {},
        "realized_pnl_usdt": 0.0,
        "total_trades_count": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    await save_wallet(new_wallet)
    logger.info("Initialized new trading wallet: %s KZT (~%s USDT)", initial_kzt, initial_usdt)
    return new_wallet


async def save_wallet(wallet: dict) -> None:
    """Сохраняет состояние кошелька в БД."""
    wallet["updated_at"] = datetime.now(timezone.utc).isoformat()
    await set_trade_state(WALLET_KEY, wallet, pair=WALLET_PAIR)


async def reset_wallet(deposit_kzt: float = None, rate: float = None) -> dict:
    """Сбрасывает кошелек к начальному депозиту."""
    rate = rate or USDT_KZT_RATE or 500.0
    deposit_kzt = deposit_kzt or WALLET_INITIAL_KZT or 10000.0
    deposit_usdt = round(deposit_kzt / rate, 2)
    now_iso = datetime.now(timezone.utc).isoformat()

    wallet = {
        "initial_deposit_kzt": deposit_kzt,
        "usdt_kzt_rate": rate,
        "initial_deposit_usdt": deposit_usdt,
        "cash_usdt": deposit_usdt,
        "positions": {},
        "realized_pnl_usdt": 0.0,
        "total_trades_count": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    await save_wallet(wallet)
    logger.info("Reset trading wallet to %s KZT (%s USDT)", deposit_kzt, deposit_usdt)
    return wallet


async def can_open_position(pair: str) -> tuple[bool, str]:
    """Проверяет возможность открытия новой позиции по паре."""
    wallet = await get_wallet()
    positions = wallet.get("positions", {})

    if pair in positions:
        return False, f"Позиция {pair} уже открыта"

    if len(positions) >= WALLET_MAX_POSITIONS:
        return False, f"Все слоты заняты ({len(positions)}/{WALLET_MAX_POSITIONS})"

    cash = float(wallet.get("cash_usdt", 0.0))
    if cash < WALLET_MIN_ORDER_USDT:
        return False, f"Недостаточно средств: свободно {cash:.2f} USDT (мин. {WALLET_MIN_ORDER_USDT} USDT)"

    return True, "OK"


async def calculate_buy_qty(pair: str, price: float) -> tuple[float, float]:
    """
    Рассчитывает объем покупки (qty, cost_usdt) строго в пределах свободного депозита.
    Делит свободный капитал по доступным слотам (до WALLET_MAX_POSITIONS).
    """
    if price <= 0:
        return 0.0, 0.0

    wallet = await get_wallet()
    positions = wallet.get("positions", {})
    cash = float(wallet.get("cash_usdt", 0.0))

    if cash < WALLET_MIN_ORDER_USDT:
        return 0.0, 0.0

    remaining_slots = max(1, WALLET_MAX_POSITIONS - len(positions))
    slot_budget = cash / remaining_slots
    target_budget = min(cash, max(WALLET_MIN_ORDER_USDT, slot_budget))

    raw_qty = target_budget / price
    qty = _round_qty(raw_qty, price)

    if qty * price > cash:
        step = 10 ** (-6 if price >= 10000 else -5 if price >= 1000 else -3 if price >= 50 else -2)
        qty = max(0.0, round(qty - step, 6))

    cost = round(qty * price, 4)
    if cost < WALLET_MIN_ORDER_USDT or qty <= 0:
        return 0.0, 0.0

    return qty, cost


async def allocate_buy(pair: str, price: float, qty: float) -> dict:
    """Списывает средства со свободного баланса и регистрирует позицию в кошельке."""
    wallet = await get_wallet()
    positions = wallet.setdefault("positions", {})
    cash = float(wallet.get("cash_usdt", 0.0))

    cost = round(qty * price, 4)
    if cost > cash:
        logger.warning("Cost %s exceeds cash %s for %s, capping", cost, cash, pair)
        cost = cash

    wallet["cash_usdt"] = round(cash - cost, 4)
    positions[pair] = {
        "pair": pair,
        "qty": qty,
        "entry_price": price,
        "cost_usdt": cost,
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    await save_wallet(wallet)
    logger.info("Wallet BUY: %s qty=%s at %s, cost=%s USDT. Remaining cash: %s USDT",
                pair, qty, price, cost, wallet["cash_usdt"])
    return positions[pair]


async def release_sell(pair: str, price: float, qty: float) -> dict:
    """Возвращает средства от продажи в кошелек, фиксирует PnL."""
    wallet = await get_wallet()
    positions = wallet.setdefault("positions", {})

    pos = positions.pop(pair, None)
    entry_price = float(pos.get("entry_price", price)) if pos else price
    entry_cost = float(pos.get("cost_usdt", qty * entry_price)) if pos else round(qty * entry_price, 4)

    proceeds = round(qty * price, 4)
    pnl = round(proceeds - entry_cost, 4)

    wallet["cash_usdt"] = round(float(wallet.get("cash_usdt", 0.0)) + proceeds, 4)
    wallet["realized_pnl_usdt"] = round(float(wallet.get("realized_pnl_usdt", 0.0)) + pnl, 4)
    wallet["total_trades_count"] = int(wallet.get("total_trades_count", 0)) + 1
    if pnl > 0:
        wallet["winning_trades"] = int(wallet.get("winning_trades", 0)) + 1
    elif pnl < 0:
        wallet["losing_trades"] = int(wallet.get("losing_trades", 0)) + 1

    await save_wallet(wallet)
    logger.info("Wallet SELL: %s qty=%s at %s, proceeds=%s USDT, pnl=%s USDT. Cash now: %s USDT",
                pair, qty, price, proceeds, pnl, wallet["cash_usdt"])

    return {
        "pnl": pnl,
        "proceeds": proceeds,
        "entry_cost": entry_cost,
        "entry_price": entry_price,
        "cash_usdt": wallet["cash_usdt"],
    }


async def get_wallet_summary(current_prices: dict = None) -> dict:
    """
    Формирует сводный отчет по кошельку с пересчетом в KZT и USDT.
    current_prices: словарь {pair: current_price} для оценки плавающего PnL.
    """
    wallet = await get_wallet()
    rate = float(wallet.get("usdt_kzt_rate", USDT_KZT_RATE or 500.0))
    initial_kzt = float(wallet.get("initial_deposit_kzt", WALLET_INITIAL_KZT or 10000.0))
    initial_usdt = float(wallet.get("initial_deposit_usdt", initial_kzt / rate))
    cash_usdt = float(wallet.get("cash_usdt", 0.0))
    cash_kzt = round(cash_usdt * rate, 2)

    positions = wallet.get("positions", {})
    current_prices = current_prices or {}

    pos_details = []
    positions_cost_usdt = 0.0
    positions_market_usdt = 0.0

    for pair, pos in positions.items():
        qty = float(pos.get("qty", 0.0))
        entry_price = float(pos.get("entry_price", 0.0))
        cost = float(pos.get("cost_usdt", qty * entry_price))
        positions_cost_usdt += cost

        cur_price = current_prices.get(pair, entry_price)
        cur_market = round(qty * cur_price, 4)
        positions_market_usdt += cur_market
        unrealized = round(cur_market - cost, 4)
        unrealized_pct = round((unrealized / cost * 100), 2) if cost > 0 else 0.0

        pos_details.append({
            "pair": pair,
            "qty": qty,
            "entry_price": entry_price,
            "cur_price": cur_price,
            "cost_usdt": cost,
            "market_usdt": cur_market,
            "unrealized_pnl": unrealized,
            "unrealized_pct": unrealized_pct,
        })

    equity_usdt = round(cash_usdt + positions_market_usdt, 2)
    equity_kzt = round(equity_usdt * rate, 2)
    total_pnl_usdt = round(equity_usdt - initial_usdt, 2)
    total_pnl_kzt = round(total_pnl_usdt * rate, 2)
    roi_pct = round((total_pnl_usdt / initial_usdt * 100), 2) if initial_usdt > 0 else 0.0

    return {
        "initial_kzt": initial_kzt,
        "initial_usdt": initial_usdt,
        "usdt_kzt_rate": rate,
        "cash_usdt": cash_usdt,
        "cash_kzt": cash_kzt,
        "positions_count": len(positions),
        "max_positions": WALLET_MAX_POSITIONS,
        "positions": pos_details,
        "positions_cost_usdt": round(positions_cost_usdt, 2),
        "positions_market_usdt": round(positions_market_usdt, 2),
        "equity_usdt": equity_usdt,
        "equity_kzt": equity_kzt,
        "total_pnl_usdt": total_pnl_usdt,
        "total_pnl_kzt": total_pnl_kzt,
        "roi_pct": roi_pct,
        "realized_pnl_usdt": float(wallet.get("realized_pnl_usdt", 0.0)),
        "total_trades_count": int(wallet.get("total_trades_count", 0)),
        "winning_trades": int(wallet.get("winning_trades", 0)),
        "losing_trades": int(wallet.get("losing_trades", 0)),
    }


def format_wallet_message(summary: dict) -> str:
    """Форматирует сводку кошелька в красивое HTML-сообщение."""
    pnl_sign = "+" if summary["total_pnl_usdt"] >= 0 else ""
    pnl_emoji = "🟢" if summary["total_pnl_usdt"] >= 0 else "🔴"

    pos_lines = []
    if summary["positions"]:
        for p in summary["positions"]:
            u_sign = "+" if p["unrealized_pnl"] >= 0 else ""
            pos_lines.append(
                f"  • <b>{p['pair']}</b>: {p['qty']} (вход: <code>{p['entry_price']}</code>) "
                f"→ <code>{p['market_usdt']:.2f} USDT</code> "
                f"({u_sign}{p['unrealized_pnl']:.2f}$ / {u_sign}{p['unrealized_pct']:.1f}%)"
            )
        pos_block = "\n".join(pos_lines)
    else:
        pos_block = "  <i>Все слоты свободны</i>"

    return (
        f"💼 <b>Торговый кошелек (PAPER)</b>\n\n"
        f"💰 <b>Начальный депозит:</b> <code>{summary['initial_kzt']:,.0f} ₸</code> "
        f"(<code>{summary['initial_usdt']:.2f} USDT</code>)\n"
        f"💵 <b>Свободно (Cash):</b> <code>{summary['cash_kzt']:,.0f} ₸</code> "
        f"(<code>{summary['cash_usdt']:.2f} USDT</code>)\n"
        f"📊 <b>В позициях:</b> <code>{summary['positions_market_usdt'] * summary['usdt_kzt_rate']:,.0f} ₸</code> "
        f"(<code>{summary['positions_market_usdt']:.2f} USDT</code>)\n"
        f"📈 <b>Оценка (Equity):</b> <code>{summary['equity_kzt']:,.0f} ₸</code> "
        f"(<code>{summary['equity_usdt']:.2f} USDT</code>)\n\n"
        f"{pnl_emoji} <b>Общий PnL:</b> <b>{pnl_sign}{summary['total_pnl_kzt']:,.0f} ₸ "
        f"({pnl_sign}{summary['total_pnl_usdt']:.2f} USDT / {pnl_sign}{summary['roi_pct']}%)</b>\n"
        f"🎰 <b>Слоты:</b> <code>{summary['positions_count']} / {summary['max_positions']}</code>\n"
        f"🏆 <b>Сделок закрыто:</b> {summary['total_trades_count']} "
        f"(W: {summary['winning_trades']} / L: {summary['losing_trades']})\n\n"
        f"<b>Открытые позиции:</b>\n{pos_block}\n\n"
        f"<i>Курс: 1 USDT = {summary['usdt_kzt_rate']:.0f} ₸ | Мин. сделка: {WALLET_MIN_ORDER_USDT} USDT</i>"
    )
