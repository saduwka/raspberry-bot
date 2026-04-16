import json
import logging
import html
import re
import asyncio
from config import GEMINI_API_KEY

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

def clean_html(raw_html):
    """Очищает текст от HTML тегов, оставляя только разрешенные Telegram (b, i, code, a)."""
    if not raw_html:
        return ""
    
    try:
        # Используем BeautifulSoup для корректной обработки и экранирования
        soup = BeautifulSoup(raw_html, 'html.parser')
        allowed_tags = ['b', 'i', 'code', 'a']
        
        for tag in soup.find_all(True):
            if tag.name not in allowed_tags:
                # Заменяем тег на его содержимое (текст), удаляя сам тег
                tag.unwrap()
            else:
                # Для разрешенных тегов оставляем только нужные атрибуты (например, href для 'a')
                allowed_attrs = ['href'] if tag.name == 'a' else []
                tag.attrs = {k: v for k, v in tag.attrs.items() if k in allowed_attrs}
        
        # decode_contents() возвращает строку с экранированными спецсимволами (&lt;, &amp; и т.д.),
        # при этом сохраняя разрешенные HTML-теги.
        return soup.decode_contents().strip()
    except Exception as e:
        logger.error(f"Error cleaning HTML: {e}")
        # В случае ошибки возвращаем текст без тегов и экранированный
        return html.escape(re.sub(r'<.*?>', '', raw_html))

def extract_json(text):
    """Пытается извлечь JSON из ответа Gemini, если он обернут в ```json...```"""
    try:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        return json.loads(text)
    except Exception as e:
        logger.warning(f"Failed to extract JSON from: {text[:100]}... Error: {e}")
        return None

async def process_with_gemini(title, summary, retries=2):
    """Пересказывает новость через Gemini в стиле аналитики и оценивает сентимент для трейдинга."""
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash-lite')
    
    prompt = f"""Ты — ведущий аналитик игровой и финансовой индустрии. Подготовь сжатую авторскую заметку по новости на русском языке.
Стиль: серьезный, профессиональный, "человеческий". 
Никаких эмодзи. Объем: до 1200 символов.

Текст должен быть связным и делиться на 2-3 логических абзаца:
1. Начни сразу с сути события.
2. Раскрой критические детали, используй <b>жирный текст</b> для названий.
3. Короткий экспертный вывод.

Также определи sentiment новости для рынка криптовалют/акций (влияние на индустрию):
-1: негативно (увольнения, отмены, падение акций, взломы)
0: нейтрально (обычные релизы, анонсы, плановые обновления)
1: позитивно (рекордные продажи, покупка студий, инновации)

Верни ТОЛЬКО JSON:
{{
  "title": "заголовок",
  "summary": "аналитический текст с HTML тегами <b></b>",
  "tags": ["тег1", "тег2"],
  "sentiment": 0
}}

Заголовок: {title}
Текст: {summary[:4000]}"""

    for attempt in range(retries + 1):
        try:
            response = await model.generate_content_async(prompt)
            if not response or not response.text:
                continue
                
            data = extract_json(response.text.strip())
            
            if data:
                # Валидация и очистка полей
                raw_summary = str(data.get("summary", f"<b>{title}</b>"))
                return {
                    "title": str(data.get("title", title)),
                    "summary": clean_html(raw_summary),
                    "tags": list(data.get("tags", [])),
                    "sentiment": int(data.get("sentiment", 0))
                }
            
            logger.info(f"Attempt {attempt+1}: Invalid JSON from Gemini, retrying...")
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"Attempt {attempt+1}: Gemini error: {e}")
            await asyncio.sleep(1)

    # Если всё провалилось, возвращаем заглушку
    return {
        "title": title,
        "summary": f"🎮 <b>{clean_html(title)}</b>\n\nНе удалось сгенерировать пересказ.",
        "tags": [],
        "sentiment": 0
    }

async def process_job_scoring(job_title, company, description, history=None):
    """Оценивает вакансию с учетом резюме и предыдущих предпочтений пользователя."""
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash-lite')
    
    cv_summary = "Vue 3, React, TypeScript, Microfrontends, REST API, Next.js, Git, Английский B1. Опыт: 4 года 1 мес. Локация: Казахстан (ищет Worldwide Remote)."
    
    history_context = ""
    if history:
        liked = "\n".join([f"- {h['title']} в {h['company']}" for h in history.get('liked', [])])
        disliked = "\n".join([f"- {h['title']} в {h['company']}" for h in history.get('disliked', [])])
        if liked: history_context += f"\nПользователю РАНЕЕ ПОНРАВИЛИСЬ эти вакансии:\n{liked}"
        if disliked: history_context += f"\nПользователь РАНЕЕ ОТКЛОНИЛ эти вакансии:\n{disliked}"

    prompt = f"""Ты — HR-эксперт по международному найму. Оцени вакансию для Frontend разработчика:
{job_title} в компании {company}.

Описание/Стек:
{description[:3500]}

Профиль кандидата:
{cv_summary}
{history_context}

Твоя задача:
1. Оцени стек и опыт (0-10).
2. ПРОВЕРЬ ЛОКАЦИЮ:
   - Является ли это Worldwide Remote (нанимают отовсюду)? 
   - Если есть жесткое ограничение по стране (кроме Казахстана), снижай score до 0-3.
3. УЧТИ ПРЕДПОЧТЕНИЯ:
   - Если вакансия похожа на те, что пользователю понравились, добавь +1 к score.
   - Если вакансия похожа на те, что пользователь отклонил, снижай score.

Верни ТОЛЬКО JSON:
{{
  "score": 0,
  "is_worldwide": true/false,
  "core_stack_match": true,
  "matching_skills": [],
  "missing_skills": [],
  "location_reason": "...",
  "verdict": "...",
  "has_salary": false
}}"""

    try:
        response = await model.generate_content_async(prompt)
        if not response or not response.text:
            return {"score": 0, "is_worldwide": False, "location_reason": "Error", "verdict": "Ошибка ИИ", "has_salary": False}
            
        data = extract_json(response.text.strip())
        if data:
            return {
                "score": int(data.get("score", 0)),
                "is_worldwide": bool(data.get("is_worldwide", False)),
                "core_stack_match": bool(data.get("core_stack_match", False)),
                "matching_skills": list(data.get("matching_skills", [])),
                "missing_skills": list(data.get("missing_skills", [])),
                "location_reason": str(data.get("location_reason", "N/A")),
                "verdict": str(data.get("verdict", "Не удалось проанализировать")),
                "has_salary": bool(data.get("has_salary", False))
            }
    except Exception as e:
        logger.error(f"Job scoring error: {e}")
        
    return {
        "score": 0,
        "is_worldwide": False,
        "core_stack_match": False,
        "matching_skills": [],
        "missing_skills": [],
        "location_reason": "Error",
        "verdict": "Ошибка обработки",
        "has_salary": False,
    }

async def evaluate_trade_with_gemini(pair, market_snapshot, technical_signal, avg_sentiment, retries=1):
    """
    Просит Gemini подтвердить или отклонить торговый сигнал по рынку.
    Агрессивная настройка: Gemini теперь выступает как активный трейдер.
    """
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash-lite')

    prompt = f"""Ты — профессиональный дей-трейдер, ориентированный на захват краткосрочных импульсов.
Твоя задача — подтверждать вход в сделки при малейшем подтверждении тренда.

Технический сигнал стратегии: {technical_signal}
Средний новостной sentiment за 12 часов: {avg_sentiment:.2f}

Снимок рынка:
{json.dumps(market_snapshot, ensure_ascii=False)}

Правила:
- Твоя цель: НЕ упустить прибыльное движение.
- Если технический сигнал {technical_signal} и рынок не показывает явного разворота в обратную сторону — подтверждай (BUY или SELL).
- Будь решительным. Выбирай HOLD только если на рынке полный штиль или резкий обвал против сигнала.
- Учитывай, что EMA 3 и 8 — это быстрые индикаторы для скальпинга.

Верни ТОЛЬКО JSON:
{{
  "action": "BUY/SELL/HOLD",
  "confidence": 0.0-1.0,
  "reason": "краткое объяснение"
}}"""

    for attempt in range(retries + 1):
        try:
            response = await model.generate_content_async(prompt)
            if not response or not response.text:
                continue

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

async def generate_cover_letter(job_title, company, description):
    """Генерирует лаконичное сопроводительное письмо на языке вакансии."""
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash-lite')
    
    cv_summary = "Vue 3, React, TypeScript, Microfrontends, REST API, Next.js, Git, Английский B1. Опыт: 4 года 1 мес. Локация: Казахстан (ищет Worldwide Remote)."

    prompt = f"""Ты — HR-эксперт. Напиши ОЧЕНЬ короткое сопроводительное письмо (Cover Letter).
ВАЖНО: Пиши письмо на том же языке, на котором написана вакансия ниже.

Кандидат: Sadu Nurzhan
Стек: {cv_summary}

Вакансия: {job_title} в компании {company}
Описание вакансии:
{description[:3000]}

Требования к письму:
1. Язык: Тот же, что в описании вакансии.
2. Текст должен состоять СТРОГО ИЗ ОДНОГО АБЗАЦА (3-5 предложений).
3. Акцент на: Vue 3, TypeScript и 4+ года опыта.
4. Суть: Почему мой опыт в микрофронтендах и REST API полезен для {company}.
5. Тон: Уверенный и лаконичный.
6. Никаких формальных "шапок", только само письмо.

Верни ТОЛЬКО текст письма."""

    try:
        response = await model.generate_content_async(prompt)
        if response and response.text:
            return response.text.strip()
    except Exception as e:
        logger.error(f"Cover letter generation error: {e}")
        
    return "Failed to generate cover letter. Please try again later."
