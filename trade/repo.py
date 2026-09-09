import json
from datetime import datetime, timedelta

from core.db import connect


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


async def save_trade(pair, side, price, qty, pnl, signal, sentiment):
    async with connect(commit=True) as db:
        await db.execute(
            """
            INSERT INTO trades (pair, side, price, qty, pnl, signal, sentiment)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (pair, side, price, qty, pnl, signal, sentiment),
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


async def set_trade_state(key, val, pair="GLOBAL"):
    async with connect(commit=True) as db:
        json_val = json.dumps(val)
        await db.execute(
            "INSERT OR REPLACE INTO trade_state (pair, key, value) VALUES (?, ?, ?)",
            (pair, key, json_val),
        )


async def get_trade_state(key, pair="GLOBAL"):
    async with connect() as db:
        async with db.execute(
            "SELECT value FROM trade_state WHERE pair=? AND key=?",
            (pair, key),
        ) as cursor:
            row = await cursor.fetchone()
            return _decode_trade_state_value(row[0]) if row else None


async def get_last_trade_at():
    async with connect() as db:
        async with db.execute("SELECT MAX(created_at) FROM trades") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def get_daily_trades(hours=24):
    async with connect() as db:
        time_ago = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        async with db.execute(
            """
            SELECT pair, side, price, qty, pnl, signal, sentiment, created_at
            FROM trades
            WHERE created_at > ?
            ORDER BY created_at ASC
            """,
            (time_ago,),
        ) as cursor:
            return await cursor.fetchall()
