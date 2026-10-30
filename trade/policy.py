import html
import logging
from datetime import datetime, timedelta, timezone

from config import USDT_KZT_RATE
from trade.repo import get_trade_state, set_trade_state
from trade.wallet import format_money

logger = logging.getLogger(__name__)

POLICY_KEY = "trader_policy"
LESSONS_KEY = "trader_lessons"
POLICY_VERSION = 2
LESSONS_MAX = 10
PATCH_COOLDOWN = timedelta(hours=12)
SILENCE_NOTICE_COOLDOWN = timedelta(days=2)
ESCALATE_SILENCE_DAYS = 14
ESCALATE_MAX_AGGRESSION = 3

# Агрессия меняет только частоту входов и строгость фильтра модели.
# Стоп/трейлинг закреплены значениями, проверенными бэктестом.
PRESETS = {
    1: {"breakout_bars": 30, "min_confidence": 0.70, "max_daily_buys": 2},
    2: {"breakout_bars": 20, "min_confidence": 0.60, "max_daily_buys": 3},
    3: {"breakout_bars": 20, "min_confidence": 0.50, "max_daily_buys": 4},
    4: {"breakout_bars": 15, "min_confidence": 0.45, "max_daily_buys": 5},
    5: {"breakout_bars": 15, "min_confidence": 0.40, "max_daily_buys": 6},
}

_DEFAULTS = {
    "version": POLICY_VERSION,
    "aggression": 2,
    "breakout_bars": 20,
    "min_confidence": 0.60,
    "min_atr_pct": 0.3,
    "risk_pct": 1.0,
    "atr_stop": 2.0,
    "atr_trail": 3.0,
    "max_daily_buys": 3,
    "last_patch_at": None,
    "last_patch_why": None,
    "last_silence_notice_at": None,
}

_META_KEYS = ("last_patch_at", "last_patch_why", "last_silence_notice_at")
PATCHABLE = ("breakout_bars", "min_confidence", "risk_pct", "max_daily_buys", "min_atr_pct")
_RANGES = {
    "breakout_bars": (int, 15, 40),
    "min_confidence": (float, 0.35, 0.80),
    "min_atr_pct": (float, 0.15, 1.0),
    "risk_pct": (float, 0.25, 2.0),
    "atr_stop": (float, 1.5, 3.0),
    "atr_trail": (float, 2.5, 4.0),
    "max_daily_buys": (int, 1, 6),
}


def apply_aggression(level: int) -> dict:
    level = max(1, min(5, int(level)))
    merged = dict(_DEFAULTS)
    merged["aggression"] = level
    merged.update(PRESETS[level])
    return merged


def clamp_policy(raw: dict | None) -> dict:
    incoming = raw if isinstance(raw, dict) else {}
    if incoming.get("version") != POLICY_VERSION:
        incoming = {k: incoming.get(k) for k in _META_KEYS if incoming.get(k)}
    policy = apply_aggression(_clamp_int(incoming.get("aggression", _DEFAULTS["aggression"]), 1, 5, 2))
    for key, (typ, lo, hi) in _RANGES.items():
        if key in incoming and incoming[key] is not None:
            policy[key] = (_clamp_int if typ is int else _clamp_float)(incoming[key], lo, hi, policy[key])
    for key in _META_KEYS:
        policy[key] = incoming.get(key)
    policy["version"] = POLICY_VERSION
    return policy


async def load_policy() -> dict:
    stored = await get_trade_state(POLICY_KEY, "GLOBAL")
    policy = clamp_policy(stored if isinstance(stored, dict) else None)
    if not isinstance(stored, dict) or stored.get("version") != POLICY_VERSION:
        policy["last_patch_why"] = "миграция на стратегию v2 (пробой 1h + тренд 4h)"
        policy["last_patch_at"] = datetime.now(timezone.utc).isoformat()
        await set_trade_state(POLICY_KEY, policy, "GLOBAL")
    return policy


async def save_policy(policy: dict) -> dict:
    clamped = clamp_policy(policy)
    await set_trade_state(POLICY_KEY, clamped, "GLOBAL")
    return clamped


def policy_can_patch(policy: dict) -> bool:
    last = _parse_ts(policy.get("last_patch_at"))
    return last is None or datetime.now(timezone.utc) - last >= PATCH_COOLDOWN


async def set_aggression(policy: dict, level: int, why: str) -> tuple[dict, str | None]:
    before = dict(policy)
    merged = apply_aggression(level)
    for key in ("risk_pct", "min_atr_pct"):
        merged[key] = policy.get(key, merged[key])
    merged["last_silence_notice_at"] = policy.get("last_silence_notice_at")
    merged["last_patch_at"] = datetime.now(timezone.utc).isoformat()
    merged["last_patch_why"] = why
    saved = await save_policy(merged)
    changed = _policy_delta(before, saved)
    if not changed:
        return saved, None
    return saved, (
        f"⚙️ <b>Политика трейдера обновлена</b>\n"
        f"Агрессия: <code>{saved['aggression']}/5</code>\n{changed}\n"
        f"Почему: <i>{html.escape(why)}</i>"
    )


async def apply_policy_patch(policy: dict, patch: dict | None, force: bool = False) -> tuple[dict, str | None]:
    """Патч от модели: только мягкие параметры, не чаще раза в 12 часов, агрессия ±1 и не выше 3."""
    if not isinstance(patch, dict):
        return policy, None
    if not force and not policy_can_patch(policy):
        logger.info("Ignoring policy_patch: cooldown")
        return policy, None

    before = dict(policy)
    merged = dict(policy)
    if patch.get("aggression") is not None:
        try:
            cur = int(policy.get("aggression", 2))
            want = int(patch["aggression"])
            want = max(cur - 1, min(cur + 1, want))
            want = min(want, max(cur, ESCALATE_MAX_AGGRESSION))
            if want != cur:
                merged = apply_aggression(want)
                for key in ("risk_pct", "min_atr_pct") + _META_KEYS:
                    merged[key] = policy.get(key, merged.get(key))
        except (TypeError, ValueError):
            pass
    for key in PATCHABLE:
        if patch.get(key) is not None:
            merged[key] = patch[key]

    why = str(patch.get("why") or "policy_patch").strip().replace("<", "")[:200]
    merged["last_patch_at"] = datetime.now(timezone.utc).isoformat()
    merged["last_patch_why"] = why
    saved = await save_policy(merged)
    changed = _policy_delta(before, saved)
    if not changed:
        return saved, None
    return saved, (
        f"⚙️ <b>Политика трейдера обновлена</b>\n"
        f"Агрессия: <code>{saved['aggression']}/5</code>\n{changed}\n"
        f"Почему: <i>{html.escape(why)}</i>"
    )


async def load_trade_lessons(limit: int = 5) -> list:
    stored = await get_trade_state(LESSONS_KEY, "GLOBAL")
    return stored[-limit:] if isinstance(stored, list) else []


async def append_trade_lesson(pair: str, pnl: float, lesson: str) -> list:
    stored = await get_trade_state(LESSONS_KEY, "GLOBAL")
    lessons = list(stored) if isinstance(stored, list) else []
    lessons.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "pair": pair,
        "pnl": round(float(pnl), 4),
        "lesson": str(lesson or "").strip().replace("<", "")[:400],
    })
    lessons = lessons[-LESSONS_MAX:]
    await set_trade_state(LESSONS_KEY, lessons, "GLOBAL")
    return lessons


async def review_closed_trade(policy: dict, trade_ctx: dict) -> tuple[dict, str | None]:
    """Разбор закрытой сделки моделью: урок в память, патч — только с кулдауном.

    Одиночный убыток — норма для трендовой стратегии (винрейт ~30%), поэтому
    агрессия автоматически после каждого лосса больше не снижается.
    """
    from ai.trading import analyze_closed_trade

    pnl = float(trade_ctx.get("pnl") or 0.0)
    pair = str(trade_ctx.get("pair") or "?")
    rate = USDT_KZT_RATE or 500.0
    review = await analyze_closed_trade(trade_ctx, policy)
    lesson = review.get("lesson") or f"Закрытие {pair}: pnl={format_money(pnl, rate)}"
    await append_trade_lesson(pair, pnl, lesson)

    parts = [
        f"🧠 <b>Разбор сделки: {html.escape(pair)}</b>\n"
        f"PnL: <code>{format_money(pnl, rate, signed=True)}</code>\n"
        f"<i>{html.escape(lesson)}</i>"
    ]
    policy, patch_msg = await apply_policy_patch(policy, review.get("policy_patch"))
    if patch_msg:
        parts.append(patch_msg)
    return policy, "\n\n".join(parts)


async def maybe_escalate_silence(policy: dict, silence_days: float | None) -> tuple[dict, str | None]:
    """Долгая тишина: +1 к агрессии раз в 2 дня, но не выше 3."""
    if silence_days is None or silence_days < ESCALATE_SILENCE_DAYS:
        return policy, None
    if int(policy.get("aggression", 2)) >= ESCALATE_MAX_AGGRESSION:
        return policy, None
    last_notice = _parse_ts(policy.get("last_silence_notice_at"))
    now = datetime.now(timezone.utc)
    if last_notice and now - last_notice < SILENCE_NOTICE_COOLDOWN:
        return policy, None
    policy = dict(policy, last_silence_notice_at=now.isoformat())
    return await set_aggression(
        policy, int(policy.get("aggression", 2)) + 1, f"автоэскалация: тишина {int(silence_days)} дней",
    )


def silence_days_from(last_trade_at) -> float | None:
    if not last_trade_at:
        return None
    dt = _parse_ts(last_trade_at)
    if dt is None:
        try:
            dt = datetime.strptime(str(last_trade_at)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def format_policy(policy: dict, silence_days: float | None = None) -> str:
    silence = f"{silence_days:.0f} дн." if silence_days is not None else "нет сделок в БД"
    why = html.escape(str(policy.get("last_patch_why") or "—"))
    return (
        f"⚙️ <b>Политика ИИ-трейдера (v2)</b>\n\n"
        f"Стратегия: <code>пробой 1h + тренд 4h, ATR-трейлинг</code>\n"
        f"Агрессия: <code>{policy['aggression']}/5</code>\n"
        f"Пробой: <code>{policy['breakout_bars']}</code> свечей 1h\n"
        f"Min confidence модели: <code>{policy['min_confidence']}</code>\n"
        f"Мин. волатильность: <code>{policy['min_atr_pct']}%</code>\n"
        f"Риск/сделка: <code>{policy['risk_pct']}%</code> от капитала\n"
        f"Стоп / трейлинг: <code>{policy['atr_stop']}</code> / <code>{policy['atr_trail']}</code> ATR\n"
        f"Макс. BUY/день: <code>{policy['max_daily_buys']}</code>\n"
        f"Тишина: <code>{silence}</code>\n"
        f"Последнее изменение: <i>{why}</i>"
    )


def _clamp_int(val, lo, hi, default):
    try:
        return max(lo, min(hi, int(val)))
    except (TypeError, ValueError):
        return default


def _clamp_float(val, lo, hi, default):
    try:
        return round(max(lo, min(hi, float(val))), 4)
    except (TypeError, ValueError):
        return default


def _parse_ts(val):
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _policy_delta(before: dict, after: dict) -> str:
    labels = {
        "aggression": "агрессия",
        "breakout_bars": "пробой, свечей",
        "min_confidence": "confidence",
        "min_atr_pct": "мин. ATR%",
        "risk_pct": "риск %",
        "atr_stop": "ATR stop",
        "atr_trail": "ATR trail",
        "max_daily_buys": "BUY/день",
    }
    return "\n".join(
        f"{label}: <code>{before.get(key)}</code> → <code>{after.get(key)}</code>"
        for key, label in labels.items() if before.get(key) != after.get(key)
    )
