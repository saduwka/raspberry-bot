import logging
from datetime import datetime, timedelta, timezone

from trade.repo import get_trade_state, set_trade_state

logger = logging.getLogger(__name__)

POLICY_KEY = "trader_policy"
PATCH_COOLDOWN = timedelta(hours=6)
SILENCE_NOTICE_COOLDOWN = timedelta(days=1)
BOOT_SILENCE_DAYS = 14
ESCALATE_SILENCE_DAYS = 7

PRESETS = {
    1: {"adx_min": 25, "rsi_buy_max": 55, "min_confidence": 0.70, "require_volume": True},
    2: {"adx_min": 20, "rsi_buy_max": 60, "min_confidence": 0.60, "require_volume": True},
    3: {"adx_min": 16, "rsi_buy_max": 65, "min_confidence": 0.50, "require_volume": True},
    4: {"adx_min": 12, "rsi_buy_max": 70, "min_confidence": 0.45, "require_volume": False},
    5: {"adx_min": 10, "rsi_buy_max": 75, "min_confidence": 0.40, "require_volume": False},
}

_DEFAULTS = {
    "aggression": 2,
    "adx_min": 20,
    "rsi_buy_max": 60,
    "require_volume": True,
    "min_confidence": 0.60,
    "risk_usdt": 10.0,
    "atr_stop": 2.0,
    "atr_tp": 4.0,
    "max_daily_buys": 5,
    "last_patch_at": None,
    "last_patch_why": None,
    "last_silence_notice_at": None,
    "boot_notified": False,
}


def apply_aggression(level: int) -> dict:
    level = max(1, min(5, int(level)))
    merged = dict(_DEFAULTS)
    merged["aggression"] = level
    merged.update(PRESETS[level])
    return merged


def clamp_policy(raw: dict | None) -> dict:
    incoming = raw if isinstance(raw, dict) else {}
    aggression = _clamp_int(incoming.get("aggression", _DEFAULTS["aggression"]), 1, 5, 2)
    policy = apply_aggression(aggression)

    if "adx_min" in incoming:
        policy["adx_min"] = _clamp_float(incoming.get("adx_min"), 10, 25, policy["adx_min"])
    if "rsi_buy_max" in incoming:
        policy["rsi_buy_max"] = _clamp_float(incoming.get("rsi_buy_max"), 55, 75, policy["rsi_buy_max"])
    if "min_confidence" in incoming:
        policy["min_confidence"] = _clamp_float(incoming.get("min_confidence"), 0.35, 0.75, policy["min_confidence"])
    if "require_volume" in incoming:
        policy["require_volume"] = _as_bool(incoming.get("require_volume"), policy["require_volume"])
    if "risk_usdt" in incoming:
        policy["risk_usdt"] = _clamp_float(incoming.get("risk_usdt"), 5, 20, 10)
    if "atr_stop" in incoming:
        policy["atr_stop"] = _clamp_float(incoming.get("atr_stop"), 1.2, 3.0, 2.0)
    if "atr_tp" in incoming:
        policy["atr_tp"] = _clamp_float(incoming.get("atr_tp"), 2.0, 5.0, 4.0)
    if "max_daily_buys" in incoming:
        policy["max_daily_buys"] = _clamp_int(incoming.get("max_daily_buys"), 2, 8, 5)
    policy["last_patch_at"] = incoming.get("last_patch_at")
    policy["last_patch_why"] = incoming.get("last_patch_why")
    policy["last_silence_notice_at"] = incoming.get("last_silence_notice_at")
    policy["boot_notified"] = bool(incoming.get("boot_notified", False))
    return policy


async def load_policy() -> dict:
    stored = await get_trade_state(POLICY_KEY, "GLOBAL")
    return clamp_policy(stored if isinstance(stored, dict) else None)


async def save_policy(policy: dict) -> dict:
    clamped = clamp_policy(policy)
    await set_trade_state(POLICY_KEY, clamped, "GLOBAL")
    return clamped


def policy_can_patch(policy: dict) -> bool:
    last = _parse_ts(policy.get("last_patch_at"))
    if last is None:
        return True
    return datetime.now(timezone.utc) - last >= PATCH_COOLDOWN


async def apply_policy_patch(policy: dict, patch: dict | None) -> tuple[dict, str | None]:
    """Merge AI patch. Returns (policy, notify_text_or_none)."""
    if not isinstance(patch, dict):
        return policy, None
    if not policy_can_patch(policy):
        logger.info("Ignoring policy_patch: cooldown")
        return policy, None

    before = dict(policy)
    merged = dict(policy)
    if "aggression" in patch and patch["aggression"] is not None:
        merged = apply_aggression(patch["aggression"])
        for key in ("last_patch_at", "last_patch_why", "last_silence_notice_at", "boot_notified"):
            merged[key] = policy.get(key)
        for key in ("adx_min", "rsi_buy_max", "min_confidence", "require_volume",
                    "risk_usdt", "atr_stop", "atr_tp", "max_daily_buys"):
            if key in patch and patch[key] is not None:
                merged[key] = patch[key]
    else:
        for key in ("adx_min", "rsi_buy_max", "min_confidence", "require_volume",
                    "risk_usdt", "atr_stop", "atr_tp", "max_daily_buys"):
            if key in patch and patch[key] is not None:
                merged[key] = patch[key]

    why = str(patch.get("why") or "policy_patch").strip()[:200]
    merged["last_patch_at"] = datetime.now(timezone.utc).isoformat()
    merged["last_patch_why"] = why
    saved = await save_policy(merged)

    changed = _policy_delta(before, saved)
    if not changed:
        return saved, None
    text = (
        f"⚙️ <b>Политика трейдера обновлена</b>\n"
        f"Агрессия: <code>{saved['aggression']}/5</code>\n"
        f"{changed}\n"
        f"Почему: <i>{why}</i>"
    )
    return saved, text


async def maybe_boot_silence(policy: dict, silence_days: float | None) -> tuple[dict, str | None]:
    if policy.get("boot_notified"):
        return policy, None
    effective_silence = 999.0 if silence_days is None else silence_days
    if effective_silence < BOOT_SILENCE_DAYS:
        policy["boot_notified"] = True
        return await save_policy(policy), None

    policy = apply_aggression(max(int(policy.get("aggression", 2)), 4))
    policy["boot_notified"] = True
    policy["last_patch_at"] = datetime.now(timezone.utc).isoformat()
    policy["last_patch_why"] = f"тишина {int(effective_silence)} дней при старте"
    saved = await save_policy(policy)
    text = (
        f"⚙️ <b>Тишина {int(effective_silence)} дней</b>\n"
        f"Агрессия поднята до <code>{saved['aggression']}/5</code>.\n"
        f"ADX min <code>{saved['adx_min']}</code>, conf <code>{saved['min_confidence']}</code>."
    )
    return saved, text


async def maybe_escalate_silence(policy: dict, silence_days: float | None) -> tuple[dict, str | None]:
    if silence_days is None or silence_days < ESCALATE_SILENCE_DAYS:
        return policy, None
    if int(policy.get("aggression", 2)) >= 5:
        return policy, None

    last_notice = _parse_ts(policy.get("last_silence_notice_at"))
    now = datetime.now(timezone.utc)
    if last_notice and now - last_notice < SILENCE_NOTICE_COOLDOWN:
        return policy, None

    new_level = min(5, int(policy.get("aggression", 2)) + 1)
    merged = apply_aggression(new_level)
    for key in ("boot_notified",):
        merged[key] = policy.get(key)
    merged["last_patch_at"] = now.isoformat()
    merged["last_patch_why"] = f"автоэскалация: тишина {int(silence_days)} дней"
    merged["last_silence_notice_at"] = now.isoformat()
    saved = await save_policy(merged)
    text = (
        f"⚙️ <b>Тишина {int(silence_days)} дней</b>\n"
        f"Агрессия {policy.get('aggression')} → <code>{saved['aggression']}/5</code>.\n"
        f"ADX min <code>{saved['adx_min']}</code>, conf <code>{saved['min_confidence']}</code>."
    )
    return saved, text


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
    why = policy.get("last_patch_why") or "—"
    return (
        f"⚙️ <b>Политика ИИ-трейдера</b>\n\n"
        f"Агрессия: <code>{policy['aggression']}/5</code>\n"
        f"ADX min: <code>{policy['adx_min']}</code>\n"
        f"RSI buy max: <code>{policy['rsi_buy_max']}</code>\n"
        f"Min confidence: <code>{policy['min_confidence']}</code>\n"
        f"Объём обязателен: <code>{'да' if policy['require_volume'] else 'нет'}</code>\n"
        f"Риск/сделка: <code>{policy['risk_usdt']} USDT</code>\n"
        f"ATR stop/tp: <code>{policy['atr_stop']}</code> / <code>{policy['atr_tp']}</code>\n"
        f"Макс. BUY/день: <code>{policy['max_daily_buys']}</code>\n"
        f"Тишина: <code>{silence}</code>\n"
        f"Последнее изменение: <i>{why}</i>"
    )


def _as_bool(val, default=True):
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "da", "да"}
    return default


def _clamp_int(val, lo, hi, default):
    try:
        return max(lo, min(hi, int(val)))
    except (TypeError, ValueError):
        return default


def _clamp_float(val, lo, hi, default):
    try:
        return max(lo, min(hi, float(val)))
    except (TypeError, ValueError):
        return default


def _parse_ts(val):
    if not val:
        return None
    try:
        text = str(val).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _policy_delta(before: dict, after: dict) -> str:
    labels = {
        "aggression": "агрессия",
        "adx_min": "ADX min",
        "rsi_buy_max": "RSI max",
        "min_confidence": "confidence",
        "require_volume": "объём",
        "risk_usdt": "риск",
        "atr_stop": "ATR stop",
        "atr_tp": "ATR tp",
        "max_daily_buys": "BUY/день",
    }
    parts = []
    for key, label in labels.items():
        if before.get(key) != after.get(key):
            parts.append(f"{label}: <code>{before.get(key)}</code> → <code>{after.get(key)}</code>")
    return "\n".join(parts)
