import json

from core.db import connect

_columns_ready = False


def _decode_trade_state_value(val):
    if val is None or not isinstance(val, str):
        return val
    raw = val.strip()
    if raw == "":
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw


async def _ensure_columns(db):
    """Миграция trades: fee, entry_price, reason (идемпотентно)."""
    global _columns_ready
    if _columns_ready:
        return
    async with db.execute("PRAGMA table_info(trades)") as cur:
        existing = {row[1] for row in await cur.fetchall()}
    for name, ddl in (("fee", "REAL DEFAULT 0"), ("entry_price", "REAL"), ("reason", "TEXT")):
        if name not in existing:
            await db.execute(f"ALTER TABLE trades ADD COLUMN {name} {ddl}")
    _columns_ready = True


async def save_trade(pair, side, price, qty, pnl, signal, sentiment, fee=0.0, entry_price=None, reason=None):
    async with connect(commit=True) as db:
        await _ensure_columns(db)
        await db.execute(
            """
            INSERT INTO trades (pair, side, price, qty, pnl, signal, sentiment, fee, entry_price, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (pair, side, price, qty, pnl, signal, sentiment, fee, entry_price, reason),
        )


async def get_open_position(pair):
    return await get_trade_state("current_position", pair)


async def get_trade_stats(days=7, pair=None):
    async with connect() as db:
        query = "SELECT COUNT(*), SUM(pnl) FROM trades WHERE created_at > datetime('now', ?)"
        params = [f"-{days} days"]
        if pair:
            query += " AND pair = ?"
            params.append(pair)
        async with db.execute(query, params) as cursor:
            return await cursor.fetchone()


async def get_closed_trades(days=None, pair=None, limit=None, since=None):
    """Закрытые сделки (SELL) — новые первыми.

    Строка: (pair, price, qty, pnl, fee, entry_price, reason, signal, created_at)
    """
    async with connect(commit=True) as db:
        await _ensure_columns(db)
        query = (
            "SELECT pair, price, qty, pnl, COALESCE(fee, 0), entry_price, reason, signal, created_at "
            "FROM trades WHERE side = 'SELL'"
        )
        params = []
        if days is not None:
            query += " AND created_at > datetime('now', ?)"
            params.append(f"-{int(days)} days")
        if since is not None:
            query += " AND created_at >= ?"
            params.append(since)
        if pair:
            query += " AND pair = ?"
            params.append(pair)
        query += " ORDER BY id DESC"
        if limit:
            query += " LIMIT ?"
            params.append(int(limit))
        async with db.execute(query, params) as cursor:
            return await cursor.fetchall()


async def set_trade_state(key, val, pair="GLOBAL"):
    async with connect(commit=True) as db:
        await db.execute(
            "INSERT OR REPLACE INTO trade_state (pair, key, value) VALUES (?, ?, ?)",
            (pair, key, json.dumps(val)),
        )


async def get_trade_state(key, pair="GLOBAL"):
    async with connect() as db:
        async with db.execute(
            "SELECT value FROM trade_state WHERE pair=? AND key=?",
            (pair, key),
        ) as cursor:
            row = await cursor.fetchone()
            return _decode_trade_state_value(row[0]) if row else None


async def clear_pair_state(pairs, keys=None):
    keys = keys or ("current_position", "entry_price", "position_qty", "highest_price", "entry_atr")
    async with connect(commit=True) as db:
        for pair in pairs:
            for key in keys:
                await db.execute("DELETE FROM trade_state WHERE pair=? AND key=?", (pair, key))


async def get_last_trade_at():
    async with connect() as db:
        async with db.execute("SELECT MAX(created_at) FROM trades") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def get_daily_trades(hours=24):
    """Сделки за N часов. created_at хранится в UTC (CURRENT_TIMESTAMP), сравниваем в SQLite.

    Строка: (pair, side, price, qty, pnl, signal, sentiment, created_at, fee, reason)
    """
    async with connect(commit=True) as db:
        await _ensure_columns(db)
        async with db.execute(
            """
            SELECT pair, side, price, qty, pnl, signal, sentiment, created_at, COALESCE(fee, 0), reason
            FROM trades
            WHERE created_at > datetime('now', ?)
            ORDER BY created_at ASC
            """,
            (f"-{int(hours)} hours",),
        ) as cursor:
            return await cursor.fetchall()
