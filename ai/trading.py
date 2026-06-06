import json
import logging
import asyncio
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

Твоя задача:
1. Кратко оцени общую эффективность (профит/убыток, винрейт).
2. Выдели 1-2 ключевых момента (удачные входы или ошибки).
3. Дай совет на завтра.

Стиль: профессиональный, лаконичный, без воды. Используй HTML-теги <b> и <code> для Telegram.
Никаких приветствий, начни сразу с заголовка <b>📊 Итоги торгового дня</b>."""

    try:
        response = await model.generate_content_async(prompt)
        if response and response.text:
            return clean_html(response.text.strip())
    except Exception as e:
        logger.error(f"Daily analytics generation error: {e}")
    return "Не удалось сгенерировать аналитику за сегодня."
