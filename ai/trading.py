import json
import logging
import asyncio

from ai import client as local_llm
from core.html import clean_html
from core.jsonutil import extract_json

logger = logging.getLogger(__name__)


def _normalize_action(value) -> str:
    action = str(value or "HOLD").upper()
    return action if action in {"BUY", "SELL", "HOLD"} else "HOLD"


def _normalize_confidence(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


async def evaluate_cycle(snapshots, policy, stats, retries=1):
    """Один вызов модели на все пары: решения + опциональный policy_patch."""
    logger.info("Trade cycle LLM start pairs=%s aggression=%s",
                [s.get("pair") for s in snapshots], policy.get("aggression"))
    prompt = f"""Ты управляешь крипто-ботом. Техсигнал — совет, не вето. Ты решаешь BUY/SELL/HOLD.
Торгуй внутри лимитов. HOLD только если нет края. Не будь параноиком.
Если days_silent >= 7 — обязан предложить policy_patch с большей aggression.

Политика сейчас:
{json.dumps({k: policy.get(k) for k in ("aggression","adx_min","rsi_buy_max","require_volume","min_confidence","risk_usdt","atr_stop","atr_tp","max_daily_buys")}, ensure_ascii=False)}

Статистика:
{json.dumps(stats, ensure_ascii=False)}

Снимки рынка:
{json.dumps(snapshots, ensure_ascii=False, default=str)}

Правила:
- SELL без позиции бессмысленен — ставь HOLD.
- BUY при уже открытой позиции — HOLD (не удваивать).
- risk_usdt в 5..20. Стоп нельзя выключить.
- policy_patch не обязателен, кроме тишины >= 7 дней.

Верни ТОЛЬКО JSON:
{{
  "decisions": [
    {{"pair": "ETH/USDT", "action": "HOLD", "confidence": 0.4, "reason": "...", "risk_usdt": 10}}
  ],
  "policy_patch": null
}}
policy_patch пример: {{"aggression": 4, "adx_min": 12, "why": "0 trades in 14d"}}
"""

    for attempt in range(retries + 1):
        try:
            text = await local_llm.chat(prompt)
            if not text:
                logger.error("Cycle local attempt %s: %s", attempt + 1, local_llm.last_error)
                await asyncio.sleep(1)
                continue

            logger.info("Trade cycle %s raw: %s", local_llm.model_label(), text[:400])
            data = extract_json(text)
            if not isinstance(data, dict):
                logger.info("Trade cycle attempt %s: invalid JSON, retrying...", attempt + 1)
                await asyncio.sleep(1)
                continue

            decisions = []
            raw_list = data.get("decisions")
            if not isinstance(raw_list, list):
                raw_list = []
            by_pair = {}
            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                pair = str(item.get("pair") or "").strip()
                if not pair:
                    continue
                risk = item.get("risk_usdt", policy.get("risk_usdt", 10))
                try:
                    risk = float(risk)
                except (TypeError, ValueError):
                    risk = policy.get("risk_usdt", 10)
                by_pair[pair] = {
                    "pair": pair,
                    "action": _normalize_action(item.get("action")),
                    "confidence": _normalize_confidence(item.get("confidence")),
                    "reason": str(item.get("reason") or "нет объяснения").strip()[:300],
                    "risk_usdt": risk,
                    "provider": local_llm.model_label(),
                }

            for snap in snapshots:
                pair = snap["pair"]
                if pair in by_pair:
                    decisions.append(by_pair[pair])
                else:
                    decisions.append({
                        "pair": pair,
                        "action": "HOLD",
                        "confidence": 0.0,
                        "reason": "модель не вернула решение по паре",
                        "risk_usdt": policy.get("risk_usdt", 10),
                        "provider": local_llm.model_label(),
                    })

            patch = data.get("policy_patch")
            if patch is not None and not isinstance(patch, dict):
                patch = None

            return {"decisions": decisions, "policy_patch": patch, "ok": True}
        except Exception as e:
            logger.error("Trade cycle attempt %s: %s", attempt + 1, e)
            await asyncio.sleep(1)

    fallback = []
    min_conf = float(policy.get("min_confidence") or 0.5)
    for snap in snapshots:
        hint = snap.get("technical_signal") or "HOLD"
        action = hint if hint in {"BUY", "SELL"} else "HOLD"
        fallback.append({
            "pair": snap["pair"],
            "action": action,
            "confidence": min_conf if action in {"BUY", "SELL"} else 0.0,
            "reason": f"битый JSON/модель недоступна, tech hint={hint} ({local_llm.last_error or 'нет ответа'})",
            "risk_usdt": policy.get("risk_usdt", 10),
            "provider": local_llm.model_label(),
        })
    return {"decisions": fallback, "policy_patch": None, "ok": False}


async def generate_daily_analytics(trades_summary, policy=None, silence_days=None):
    """Генерирует аналитический отчет по итогам торгового дня через локальную модель."""
    extra = ""
    if policy is not None:
        extra = (
            f"\nПолитика: aggression={policy.get('aggression')}, "
            f"тишина={silence_days if silence_days is not None else 'n/a'} дней.\n"
            "Если сделок 0 и тишина >= 7 — в конце дай рекомендацию поднять aggression."
        )
    prompt = f"""Ты — главный аналитик торгового фонда. Подведи итоги торгового дня на основе списка сделок.
Список сделок за сегодня (JSON):
{json.dumps(trades_summary, ensure_ascii=False)}
{extra}

Твоя задача:
1. Кратко оцени общую эффективность (профит/убыток, винрейт).
2. Выдели 1-2 ключевых момента (удачные входы или ошибки).
3. Дай совет на завтра по агрессии.

Стиль: профессиональный, лаконичный, без воды. Используй HTML-теги <b> и <code> для Telegram.
Никаких приветствий, начни сразу с заголовка <b>📊 Итоги торгового дня</b>."""

    try:
        text = await local_llm.chat(prompt)
        if text:
            return clean_html(text)
        logger.error("Daily analytics local failed: %s", local_llm.last_error)
    except Exception as e:
        logger.error("Daily analytics generation error: %s", e)
    return (
        f"Не удалось сгенерировать аналитику: локальная модель недоступна "
        f"({local_llm.last_error or 'нет ответа'})."
    )
