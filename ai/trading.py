import json
import logging
import asyncio

from ai import client as local_llm
from core.html import clean_html, ensure_valid_html
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


POLICY_KEYS = ("aggression", "breakout_bars", "min_confidence", "min_atr_pct", "risk_pct",
               "atr_stop", "atr_trail", "max_daily_buys")


async def evaluate_cycle(snapshots, policy, stats, retries=1):
    """Модель — фильтр (вето): подтверждает или отклоняет BUY-кандидатов техстратегии."""
    logger.info("Trade cycle LLM start candidates=%s aggression=%s",
                [s.get("pair") for s in snapshots], policy.get("aggression"))
    prompt = f"""Ты — риск-фильтр крипто-бота (спот, только long). Техстратегия уже дала сигнал BUY:
пробой максимума последних {policy.get("breakout_bars")} часовых свечей при восходящем тренде 4h.
Стоп и трейлинг ставятся автоматически, тейка нет — прибыль даёт удержание тренда.
Винрейт такой стратегии ~30%: одиночные убытки — норма, большие выигрыши их окупают.

Твоя задача: для каждого кандидата ответить BUY (подтвердить) или HOLD (отклонить).
Отклоняй только при явной причине: RSI > 80 (перегрет), объём заметно ниже среднего на пробое
(volume_vs_avg < 0.7), цена уже далеко над EMA50 (> 3 ATR), повторение ошибки из recent_lessons.
Не торгуй пары, которых нет в списке кандидатов.

Политика:
{json.dumps({k: policy.get(k) for k in POLICY_KEYS}, ensure_ascii=False)}

Статистика:
{json.dumps(stats, ensure_ascii=False, default=str)}

Кандидаты:
{json.dumps(snapshots, ensure_ascii=False, default=str)}

policy_patch — почти всегда null. Допустимые поля: min_confidence, breakout_bars, max_daily_buys, why.

Верни ТОЛЬКО JSON:
{{
  "decisions": [
    {{"pair": "ETH/USDT", "action": "BUY", "confidence": 0.7, "reason": "коротко по-русски"}}
  ],
  "policy_patch": null
}}
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
                await asyncio.sleep(1)
                continue
            by_pair = {}
            for item in data.get("decisions") or []:
                if not isinstance(item, dict):
                    continue
                pair = str(item.get("pair") or "").strip()
                if pair:
                    by_pair[pair] = {
                        "pair": pair,
                        "action": "BUY" if _normalize_action(item.get("action")) == "BUY" else "HOLD",
                        "confidence": _normalize_confidence(item.get("confidence")),
                        "reason": str(item.get("reason") or "нет объяснения").strip()[:300],
                        "provider": local_llm.model_label(),
                    }
            decisions = [by_pair.get(s["pair"]) or {
                "pair": s["pair"], "action": "BUY",
                "confidence": float(policy.get("min_confidence") or 0.5),
                "reason": "модель не дала решения по паре, вход по техсигналу",
                "provider": local_llm.model_label(),
            } for s in snapshots]
            patch = data.get("policy_patch")
            return {"decisions": decisions, "policy_patch": patch if isinstance(patch, dict) else None, "ok": True}
        except Exception as e:
            logger.error("Trade cycle attempt %s: %s", attempt + 1, e)
            await asyncio.sleep(1)

    return {
        "decisions": [{
            "pair": s["pair"], "action": "BUY", "confidence": float(policy.get("min_confidence") or 0.5),
            "reason": f"модель недоступна ({local_llm.last_error or 'нет ответа'}), вход по техсигналу",
            "provider": local_llm.model_label(),
        } for s in snapshots],
        "policy_patch": None,
        "ok": False,
    }


async def analyze_closed_trade(trade_ctx: dict, policy: dict, retries=1) -> dict:
    """Разбор закрытой сделки: lesson + optional policy_patch."""
    pnl = float(trade_ctx.get("pnl") or 0.0)
    outcome = "profit" if pnl > 0 else ("loss" if pnl < 0 else "flat")
    prompt = f"""Ты — тренер торгового бота. Разбери закрытую сделку и извлеки урок.

Сделка:
{json.dumps(trade_ctx, ensure_ascii=False, default=str)}

Политика сейчас:
{json.dumps({k: policy.get(k) for k in POLICY_KEYS}, ensure_ascii=False)}

Исход: {outcome} (pnl={pnl}, уже за вычетом комиссий).
Стратегия: пробой 1h в тренде 4h, выход только по стопу/трейлингу. Винрейт ~30% — это норма.

Правила:
- lesson: 1-2 предложения по-русски, без символа <. Что было в рынке и стоит ли что-то фильтровать.
- policy_patch почти всегда null. Одиночный убыток — НЕ повод менять политику.
- Допустимые поля патча: min_confidence, breakout_bars, max_daily_buys, why. Не поднимай агрессию после лосса.

Верни ТОЛЬКО JSON:
{{"lesson": "...", "policy_patch": null}}
"""

    for attempt in range(retries + 1):
        try:
            text = await local_llm.chat(prompt)
            if not text:
                logger.error("Closed-trade review attempt %s: %s", attempt + 1, local_llm.last_error)
                await asyncio.sleep(1)
                continue
            data = extract_json(text)
            if not isinstance(data, dict):
                logger.info("Closed-trade review attempt %s: invalid JSON", attempt + 1)
                await asyncio.sleep(1)
                continue
            lesson = str(data.get("lesson") or "").strip().replace("<", "")[:400]
            patch = data.get("policy_patch")
            if patch is not None and not isinstance(patch, dict):
                patch = None
            if patch and pnl < 0 and patch.get("aggression") is not None:
                try:
                    if int(patch["aggression"]) > int(policy.get("aggression", 2)):
                        patch["aggression"] = policy.get("aggression", 2)
                except (TypeError, ValueError):
                    pass
            if not lesson:
                lesson = f"Закрытие {trade_ctx.get('pair')}: pnl={pnl:.2f} USDT"
            return {"lesson": lesson, "policy_patch": patch, "ok": True}
        except Exception as e:
            logger.error("Closed-trade review attempt %s: %s", attempt + 1, e)
            await asyncio.sleep(1)

    return {
        "lesson": f"Закрытие {trade_ctx.get('pair')}: pnl={pnl:.2f} USDT (LLM недоступна)",
        "policy_patch": None,
        "ok": False,
    }


async def generate_daily_analytics(trades_summary, policy=None, silence_days=None, usdt_kzt_rate=None):
    """Генерирует аналитический отчет по итогам торгового дня через локальную модель."""
    from trade.wallet import format_money

    rate = float(usdt_kzt_rate) if usdt_kzt_rate else None
    enriched = []
    for t in trades_summary:
        item = dict(t)
        pnl = float(item.get("pnl") or 0.0)
        item["pnl_display"] = format_money(pnl, rate, signed=True)
        enriched.append(item)

    extra = ""
    if policy is not None:
        extra = (
            f"\nПолитика: aggression={policy.get('aggression')}, "
            f"тишина={silence_days if silence_days is not None else 'n/a'} дней.\n"
            "Отсутствие сделок во флэте/даунтренде — это норма, не советуй поднимать агрессию только из-за тишины."
        )
    prompt = f"""Ты — главный аналитик торгового фонда. Подведи итоги торгового дня на основе списка сделок.
Список сделок за сегодня (JSON):
{json.dumps(enriched, ensure_ascii=False)}
{extra}

Твоя задача:
1. Кратко оцени общую эффективность (профит/убыток, винрейт).
2. Выдели 1-2 ключевых момента (удачные входы или ошибки, причины выходов, доля комиссий).
3. Дай короткий вывод на завтра. Помни: стратегия трендовая, винрейт ~30%, отсутствие сделок во флэте — норма.

ВАЖНО: любые денежные суммы пиши ТОЛЬКО в формате из поля pnl_display, например «-0.02 USDT (-10 ₸)».
Никогда не пиши голый USDT без тенге в скобках.

Стиль: профессиональный, лаконичный, без воды. Используй HTML-теги <b> и <code> для Telegram.
Никаких приветствий, начни сразу с заголовка <b>📊 Итоги торгового дня</b>."""

    try:
        text = await local_llm.chat(prompt)
        if text:
            cleaned = clean_html(text)
            if cleaned.strip():
                logger.debug("Daily analytics cleaned HTML: %s", cleaned[:100] + "..." if len(cleaned) > 100 else cleaned)
                return ensure_valid_html(cleaned)
        logger.error("Daily analytics local failed: %s", local_llm.last_error)
        return (
            f"Не удалось сгенерировать аналитику: локальная модель недоступна "
            f"({local_llm.last_error or 'нет ответа'})."
        )
    except Exception as e:
        logger.error("Daily analytics generation error: %s", e)
        return f"Не удалось сгенерировать аналитику: {e}."

