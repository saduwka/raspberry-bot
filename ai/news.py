import json
import logging
import asyncio
from config import GEMINI_API_KEY
from ai.base import extract_json, clean_html

logger = logging.getLogger(__name__)

async def process_with_gemini(title, summary, retries=2):
    """Пересказывает новость через Gemini в стиле аналитики и оценивает сентимент для трейдинга."""
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash')
    
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

    return {
        "title": title,
        "summary": f"🎮 <b>{clean_html(title)}</b>\n\nНе удалось сгенерировать пересказ.",
        "tags": [],
        "sentiment": 0
    }
