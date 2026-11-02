"""Стратегия v2: пробой N-часового максимума по 1h в направлении тренда 4h, выход по ATR-трейлингу.

Модуль чистый (без БД и сети): одинаково используется в боте и в бэктесте
(scripts/trade_backtest.py). Параметры подобраны на годе истории BTC/ETH/SOL с комиссией
0.1% и проскальзыванием 0.05% на сторону.
"""
import math

from trade import indicators as ind

LTF_DEFAULT = "1h"
HTF_DEFAULT = "4h"


def ok(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def compute(candles):
    close = [c["close"] for c in candles]
    high = [c["high"] for c in candles]
    low = [c["low"] for c in candles]
    vol = [c["volume"] for c in candles]
    adx_v, dip, din = ind.adx(high, low, close, 14)
    return {
        "ts": [c["ts"] for c in candles],
        "close_ts": [c["close_ts"] for c in candles],
        "close": close,
        "high": high,
        "low": low,
        "volume": vol,
        "ema_trend": ind.ema(close, 50),
        "rsi": ind.rsi(close, 14),
        "atr": ind.atr(high, low, close, 14),
        "adx": adx_v,
        "di_pos": dip,
        "di_neg": din,
        "vol_sma": ind.sma(vol, 20),
    }


def compute_htf(candles):
    close = [c["close"] for c in candles]
    return {
        "close_ts": [c["close_ts"] for c in candles],
        "close": close,
        "ema20": ind.ema(close, 20),
        "ema50": ind.ema(close, 50),
    }


def htf_trend_at(htf, i):
    if i is None or i < 0:
        return None
    c, e20, e50 = htf["close"][i], htf["ema20"][i], htf["ema50"][i]
    if not (ok(e20) and ok(e50)):
        return None
    if c > e50 and e20 > e50:
        return "up"
    if c < e50 and e20 < e50:
        return "down"
    return "flat"


def breakout_level(ltf, i, bars):
    if i < bars:
        return None
    return max(ltf["high"][i - bars:i])


def entry_checks(ltf, i, htf_trend, policy, sentiment=0.0):
    """Список (название, выполнено) для входа на закрытой свече i."""
    bars = int(policy.get("breakout_bars", 20))
    min_atr_pct = float(policy.get("min_atr_pct", 0.3))
    close = ltf["close"][i]
    atr_v = ltf["atr"][i]
    level = breakout_level(ltf, i, bars)
    atr_pct = atr_v / close * 100 if ok(atr_v) and close else 0.0
    return [
        ("тренд 4h вверх", htf_trend == "up"),
        (f"пробой максимума {bars} свечей", level is not None and close > level),
        (f"волатильность ATR ≥ {min_atr_pct:g}%", atr_pct >= min_atr_pct),
        ("фон новостей не негативный", float(sentiment or 0) >= -0.3),
    ]


def entry_signal(ltf, i, htf_trend, policy, sentiment=0.0):
    if i < 60 or not ok(ltf["atr"][i]):
        return "HOLD", ["мало данных"]
    checks = entry_checks(ltf, i, htf_trend, policy, sentiment)
    failed = [name for name, passed in checks if not passed]
    if not failed:
        return "BUY", ["все условия входа выполнены"]
    return "HOLD", ["не выполнено: " + ", ".join(failed)]


def stop_level(entry, atr_entry, highest, atr_now, policy):
    """Текущий стоп: max(начальный стоп, максимум − trail·ATR). Без фиксированного тейка."""
    atr_stop = float(policy.get("atr_stop", 2.0))
    atr_trail = float(policy.get("atr_trail", 3.0))
    initial = entry - atr_entry * atr_stop
    stop, stage = initial, "initial"
    if highest is not None and atr_now:
        trail = highest - atr_now * atr_trail
        if trail > stop:
            stop, stage = trail, "trailing"
    return {"stop": stop, "initial": initial, "stage": stage, "risk": entry - initial}


EXIT_LABELS = {
    "STOP_LOSS": "стоп-лосс",
    "TRAILING_STOP": "трейлинг-стоп",
    "MANUAL": "ручное закрытие",
    "DAILY_LOSS": "дневной лимит убытка",
}


def exit_reason_for(stage):
    return "TRAILING_STOP" if stage == "trailing" else "STOP_LOSS"


def snapshot(pair, ltf, i, htf_trend, policy):
    def r(v, n=4):
        return round(float(v), n) if ok(v) else None
    close = ltf["close"][i]
    atr_v = ltf["atr"][i]
    level = breakout_level(ltf, i, int(policy.get("breakout_bars", 20)))
    return {
        "pair": pair,
        "price": r(close),
        "breakout_level": r(level),
        "dist_to_breakout_pct": r((level - close) / close * 100, 3) if level else None,
        "ema50": r(ltf["ema_trend"][i]),
        "rsi": r(ltf["rsi"][i], 2),
        "adx": r(ltf["adx"][i], 2),
        "atr": r(atr_v),
        "atr_pct": r(atr_v / close * 100, 3) if ok(atr_v) and close else None,
        "volume_vs_avg": r(ltf["volume"][i] / ltf["vol_sma"][i], 2) if ok(ltf["vol_sma"][i]) and ltf["vol_sma"][i] else None,
        "htf_trend": htf_trend,
    }
