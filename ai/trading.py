import json
import logging
import asyncio
<<<<<<< HEAD
from config import GEMINI_API_KEY
from ai.base import extract_json, clean_html

logger = logging.getLogger(__name__)

async def evaluate_trade_with_gemini(pair, market_snapshot, technical_signal, avg_sentiment, retries=1):
    """
    Просит Gemini подтвердить или отклонить торговый сигнал.
    Использует промпт Quantum Trader.
    """
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    # Здесь можно настроить модель специфично для трейдинга
    model = genai.GenerativeModel('gemini-2.5-flash')

    prompt = f"""Ты — профессиональный квантовый трейдер с 10+ годами опыта на крипто-рынках.
Твоя задача — принять ОКОНЧАТЕЛЬНОЕ решение по сделке, игнорируя шум и ложные пробои.

📊 ДАННЫЕ РЫНКА:
Пара: {pair}
Цена: {market_snapshot['price']} USDT
Объем 24ч: {market_snapshot['volume']}

📈 ТЕХНИЧЕСКИЕ ИНДИКАТОРЫ:
- EMA(5): {market_snapshot['ema_fast']}
- EMA(13): {market_snapshot['ema_slow']}
- EMA(50/тренд): {market_snapshot['ema_trend']}
- RSI(14): {market_snapshot['rsi']}
- Разрыв EMA: {market_snapshot['ema_gap']} ({abs(market_snapshot['ema_gap'])/market_snapshot['price']*100:.3f}%)

🎯 ТЕКУЩАЯ СИТУАЦИЯ:
- Технический сигнал: {technical_signal}
- Позиция: {market_snapshot['position_state']}
- Цена входа: {market_snapshot['entry_price'] or 'Нет открытой позиции'}
- Риск-выход: {market_snapshot['risk_exit'] or 'Не сработал'}
- Новостной sentiment (12ч): {avg_sentiment:.2f} (от -1 до +1)

⚠️ КРИТИЧЕСКИЕ ПРАВИЛА АНАЛИЗА:

1️⃣ ОПРЕДЕЛЕНИЕ ТРЕНДА (приоритет #1):
   • Бычий: EMA(5) > EMA(13) > EMA(50) И цена > EMA(50)
   • Медвежий: EMA(5) < EMA(13) < EMA(50) И цена < EMA(50)
   • Боковик: Разрыв EMA < 0.1% ИЛИ цена внутри EMA(13)±0.5%
   
2️⃣ ФИЛЬТР "ШУМА" (защита от распила):
   • Если разрыв EMA < 0.05% → ВСЕГДА HOLD (мертвая зона)
   • Если RSI между 45-55 И боковик → HOLD
   • Если объем ниже среднего на 30%+ → подозрительный сигнал
   
3️⃣ BUY условия (ВСЕ должны быть True):
   ✓ Четкий бычий тренд (см. п.1)
   ✓ Разрыв EMA > 0.1%
   ✓ RSI < 65 (не перекуплено)
   ✓ Цена НАД EMA(50) минимум на 0.3%
   ✓ Sentiment >= -0.2 (нет негатива в новостях)
   ✓ Нет признаков exhaustion (RSI не падает при росте цены)
   
4️⃣ SELL условия (ЛЮБОЕ из):
   ✓ Медвежий кроссовер: EMA(5) пересекла EMA(13) вниз
   ✓ Цена упала ниже EMA(50) на 0.5%+
   ✓ RSI > 75 И цена ниже EMA(5) (дивергенция)
   ✓ Сработал стоп-лосс/тейк-профит
   ✓ Sentiment упал ниже -0.5 (паника)
   
5️⃣ HOLD условия (защита капитала):
   • Любая неопределенность в данных
   • Противоречивые сигналы индикаторов
   • Недостаточный разрыв для уверенного входа
   • Подозрение на pump&dump (резкий скачок объема)

🧠 ПСИХОЛОГИЯ ТРЕЙДИНГА:
- Твой приоритет: НЕ ПОТЕРЯТЬ > Заработать
- 70% сделок могут быть HOLD — это нормально
- Лучше пропустить сделку, чем войти в trap
- Крипто = высокая волатильность, будь параноиком

📤 ОТВЕТ (строго JSON, без комментариев):
{{
  "action": "BUY/SELL/HOLD",
  "confidence": 0.0-1.0,
  "reason": "Краткое объяснение решения на основе КОНКРЕТНЫХ данных (макс 250 символов)"
}}

⚡ НАЧИНАЙ АНАЛИЗ:"""

    for attempt in range(retries + 1):
        try:
            response = await model.generate_content_async(prompt)
            if not response or not response.text:
                continue

            logger.info(f"Trade Gemini raw: {response.text.strip()[:300]}")
            data = extract_json(response.text.strip())
            if data:
                action = str(data.get("action", "HOLD")).upper()
                if action not in {"BUY", "SELL", "HOLD"}:
                    action = "HOLD"

                try:
                    confidence = float(data.get("confidence", 0.0))
                except (TypeError, ValueError):
                    confidence = 0.0
                confidence = max(0.0, min(confidence, 1.0))

                reason = str(data.get("reason", "Gemini не дал объяснение")).strip()[:300]
                
                if action in {"BUY", "SELL"} and confidence < 0.3:
                    logger.warning(f"Low confidence {confidence} for {action}, forcing HOLD")
                    action = "HOLD"
                    reason = f"Низкая уверенность ({confidence:.2f}): {reason}"
                
                return {
                    "action": action,
                    "confidence": confidence,
                    "reason": reason or "Gemini не дал объяснение",
                }

            logger.info(f"Trade Gemini attempt {attempt+1}: invalid JSON, retrying...")
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Trade Gemini attempt {attempt+1}: {e}")
            await asyncio.sleep(1)
            
    return {
        "action": "HOLD",
        "confidence": 0.0,
        "reason": "Gemini не ответил или дал некорректные данные"
    }

async def generate_daily_analytics(trades_summary):
    """Генерирует аналитический отчет по итогам торгового дня через Gemini."""
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash')

    prompt = f"""Ты — главный аналитик торгового фонда. Подведи итоги торгового дня на основе списка сделок.
Список сделок за сегодня (JSON):
{json.dumps(trades_summary, ensure_ascii=False)}
=======

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
>>>>>>> refactor/packages

Твоя задача:
1. Кратко оцени общую эффективность (профит/убыток, винрейт).
2. Выдели 1-2 ключевых момента (удачные входы или ошибки).
<<<<<<< HEAD
3. Дай совет на завтра.
=======
3. Дай совет на завтра по агрессии.
>>>>>>> refactor/packages

Стиль: профессиональный, лаконичный, без воды. Используй HTML-теги <b> и <code> для Telegram.
Никаких приветствий, начни сразу с заголовка <b>📊 Итоги торгового дня</b>."""

    try:
<<<<<<< HEAD
        response = await model.generate_content_async(prompt)
        if response and response.text:
            return clean_html(response.text.strip())
    except Exception as e:
        logger.error(f"Daily analytics generation error: {e}")
    return "Не удалось сгенерировать аналитику за сегодня."
=======
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
>>>>>>> refactor/packages
