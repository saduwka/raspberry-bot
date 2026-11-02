"""Рыночные данные Binance (публичные свечи, без ключей и без ccxt/pandas)."""
import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

DATA_HOSTS = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
)

_cache: dict = {}
_CACHE_TTL = 45


def _symbol(pair: str) -> str:
    return pair.replace("/", "").upper()


def _parse(rows):
    out = []
    for r in rows:
        out.append({
            "ts": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
            "close_ts": int(r[6]),
        })
    return out


async def fetch_klines(pair: str, interval: str, limit: int = 200, closed_only: bool = True,
                       start_ms: int | None = None, use_cache: bool = True):
    key = (pair, interval, limit, closed_only, start_ms)
    now = time.time()
    if use_cache and start_ms is None and key in _cache and now - _cache[key][0] < _CACHE_TTL:
        return _cache[key][1]

    params = {"symbol": _symbol(pair), "interval": interval, "limit": min(1000, limit + (1 if closed_only else 0))}
    if start_ms is not None:
        params["startTime"] = start_ms
    last_err = None
    async with httpx.AsyncClient(timeout=15) as client:
        for host in DATA_HOSTS:
            for attempt in range(2):
                try:
                    resp = await client.get(f"{host}/api/v3/klines", params=params)
                    resp.raise_for_status()
                    candles = _parse(resp.json())
                    if closed_only and candles and candles[-1]["close_ts"] > int(time.time() * 1000):
                        candles = candles[:-1]
                    candles = candles[-limit:] if start_ms is None else candles
                    if start_ms is None:
                        _cache[key] = (now, candles)
                    return candles
                except Exception as e:
                    last_err = e
                    await asyncio.sleep(0.5)
    logger.error("fetch_klines %s %s failed: %s", pair, interval, last_err)
    return None


async def fetch_price(pair: str):
    async with httpx.AsyncClient(timeout=10) as client:
        for host in DATA_HOSTS:
            try:
                resp = await client.get(f"{host}/api/v3/ticker/price", params={"symbol": _symbol(pair)})
                resp.raise_for_status()
                return float(resp.json()["price"])
            except Exception as e:
                logger.debug("fetch_price %s via %s failed: %s", pair, host, e)
    return None


async def fetch_prices(pairs):
    results = await asyncio.gather(*(fetch_price(p) for p in pairs), return_exceptions=True)
    return {p: r for p, r in zip(pairs, results) if isinstance(r, float)}
