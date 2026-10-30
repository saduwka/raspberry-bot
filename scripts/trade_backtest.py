"""Бэктест стратегии трейдера на реальных свечах Binance (без LLM).

python3 scripts/trade_backtest.py --days 60 --pairs BTC/USDT,ETH/USDT,SOL/USDT
"""
import argparse
import asyncio
import bisect
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trade import indicators as ind
from trade import strategy as st
from trade.market import fetch_klines

TF_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


async def history(pair, tf, days):
    cache = os.environ.get("BT_CACHE")
    if cache:
        path = os.path.join(cache, f"{pair.replace('/', '')}_{tf}_{days}.json")
        if os.path.exists(path):
            return json.load(open(path))
        data = await _history(pair, tf, days)
        os.makedirs(cache, exist_ok=True)
        json.dump(data, open(path, "w"))
        return data
    return await _history(pair, tf, days)


async def _history(pair, tf, days):
    end = int(time.time() * 1000)
    start = end - days * 86_400_000 - 120 * TF_MS[tf]
    out = []
    cur = start
    while cur < end:
        chunk = await fetch_klines(pair, tf, limit=1000, closed_only=False, start_ms=cur, use_cache=False)
        if not chunk:
            break
        out.extend(chunk)
        cur = chunk[-1]["ts"] + TF_MS[tf]
        if len(chunk) < 1000:
            break
    now = int(time.time() * 1000)
    return [c for c in out if c["close_ts"] <= now]


def stats(trades, label):
    n = len(trades)
    if not n:
        return {"label": label, "trades": 0}
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    eq = peak = dd = 0.0
    for t in trades:
        eq += t
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) else float("inf")
    return {
        "label": label,
        "trades": n,
        "winrate": round(len(wins) / n * 100, 1),
        "net_pct_sum": round(sum(trades), 2),
        "avg_pct": round(sum(trades) / n, 3),
        "profit_factor": round(pf, 2),
        "max_dd_pct": round(dd, 2),
    }


def run_v2(ltf_c, htf_c, policy, fee, slip):
    """Та же логика, что в боте: вход на открытии следующей свечи, стоп/трейлинг по свечам."""
    ltf = st.compute(ltf_c)
    htf = st.compute_htf(htf_c)
    trades = []
    pos = None
    for i in range(60, len(ltf_c) - 1):
        c = ltf_c[i]
        if pos:
            lv = st.stop_level(pos["entry"], pos["atr"], pos["highest"], ltf["atr"][i - 1], policy)
            if c["low"] <= lv["stop"]:
                px = min(lv["stop"], c["open"]) * (1 - slip)
                trades.append((px / pos["fill"] - 1 - 2 * fee) * 100)
                pos = None
            else:
                pos["highest"] = max(pos["highest"], c["high"])
            continue
        hi = bisect.bisect_right(htf["close_ts"], c["close_ts"]) - 1
        sig, _ = st.entry_signal(ltf, i, st.htf_trend_at(htf, hi), policy)
        if sig == "BUY":
            nxt = ltf_c[i + 1]["open"]
            pos = {"entry": nxt, "fill": nxt * (1 + slip), "atr": ltf["atr"][i], "highest": nxt}
    return trades


def run_old(c5, fee, slip, policy=None):
    """Порт старой логики (5m, EMA5/13/50, ADX>20, RSI<60, ATR 2/4 + трейлинг), без LLM."""
    policy = policy or {"adx_min": 20, "rsi_buy_max": 60, "require_volume": True, "atr_stop": 2.0, "atr_tp": 4.0}
    close = [c["close"] for c in c5]
    high = [c["high"] for c in c5]
    low = [c["low"] for c in c5]
    vol = [c["volume"] for c in c5]
    ef = ind.ema(close, 5); es = ind.ema(close, 13); et = ind.ema(close, 50)
    rsi = ind.rsi(close, 14); a, dp, dn = ind.adx(high, low, close, 14)
    atr = ind.atr(high, low, close, 14); vs = ind.sma(vol, 20)
    trades = []
    pos = None
    cooldown_until = -1
    for i in range(60, len(c5) - 1):
        if pos:
            pos["hi"] = max(pos["hi"], close[i])
            stop0 = pos["e"] - atr[i] * policy["atr_stop"]
            trail = pos["hi"] - atr[i] * max(1.2, policy["atr_stop"] * 0.75)
            stop = max(stop0, trail)
            tp = pos["e"] + atr[i] * policy["atr_tp"]
            bearish = ef[i] < es[i]
            cross_dn = ef[i - 1] >= es[i - 1] and bearish
            below = close[i] < et[i] * 0.998
            tech = (cross_dn or (bearish and dn[i] > dp[i]) or below) and (rsi[i] > 75 or bearish)
            if close[i] <= stop or close[i] >= tp or tech:
                px = close[i] * (1 - slip)
                trades.append(((px - pos["f"]) / pos["f"] - 2 * fee) * 100)
                pos = None
                cooldown_until = i + 3
            continue
        if i < cooldown_until:
            continue
        bull = ef[i] > es[i]
        cross_up = ef[i - 1] <= es[i - 1] and bull
        vol_ok = vol[i] > vs[i] if policy["require_volume"] else True
        if close[i] > et[i] and a[i] > policy["adx_min"] and dp[i] > dn[i]:
            if (cross_up or (bull and vol_ok)) and rsi[i] < policy["rsi_buy_max"]:
                px = close[i + 1] if False else c5[i + 1]["open"]
                pos = {"e": px, "f": px * (1 + slip), "hi": px}
    return trades


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--pairs", default="BTC/USDT,ETH/USDT,SOL/USDT")
    ap.add_argument("--fee", type=float, default=0.001)
    ap.add_argument("--slip", type=float, default=0.0005)
    ap.add_argument("--policy", default="{}", help="JSON с переопределениями политики v2")
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--ltf", default="1h")
    ap.add_argument("--htf", default="4h")
    ap.add_argument("--old", action="store_true")
    args = ap.parse_args()
    from trade.policy import _DEFAULTS
    base = dict(_DEFAULTS)
    base.update(json.loads(args.policy))
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    data = {}
    for p in pairs:
        data[p] = {"ltf": await history(p, args.ltf, args.days), "htf": await history(p, args.htf, args.days + 20)}
        c = data[p]["ltf"]
        print(f"{p}: buy&hold {((c[-1]['close']/c[120]['close'])-1)*100:.1f}% за период", flush=True)
        if args.old:
            data[p]["5m"] = await history(p, "5m", args.days)

    def evaluate(policy, label):
        allt = []
        rows = []
        for p in pairs:
            t = run_v2(data[p]["ltf"], data[p]["htf"], policy, args.fee, args.slip)
            rows.append(stats(t, p))
            allt += t
        return stats(allt, label), rows

    if args.old:
        allt = []
        for p in pairs:
            t = run_old(data[p]["5m"], args.fee, args.slip)
            print(json.dumps(stats(t, f"old {p}"), ensure_ascii=False))
            allt += t
        print(json.dumps(stats(allt, "OLD TOTAL"), ensure_ascii=False))

    if args.grid:
        results = []
        for bars in (15, 20, 30):
            for atr_stop in (1.5, 2.0, 2.5):
                for atr_trail in (2.5, 3.0, 3.5):
                    pol = dict(base, breakout_bars=bars, atr_stop=atr_stop, atr_trail=atr_trail)
                    s, _ = evaluate(pol, f"bars={bars} stop={atr_stop} trail={atr_trail}")
                    results.append(s)
        results.sort(key=lambda s: s.get("net_pct_sum", -999), reverse=True)
        for s in results[:12]:
            print(json.dumps(s, ensure_ascii=False))
        return
    s, rows = evaluate(base, "V2 TOTAL")
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    print(json.dumps(s, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
