import json
import logging
import html
import re
import asyncio
import google.generativeai as genai
from config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

genai.configure(api_key=GEMINI_API_KEY)
# Модель gemini-2.5-flash-lite (как просил пользователь, не меняем)
model = genai.GenerativeModel("gemini-2.5-flash-lite")

def clean_html(text):
    """Превращает markdown **жирный** в HTML <b> и экранирует остальное."""
    # Сначала экранируем всё
    text = html.escape(text)
    # Потом возвращаем только разрешенные теги (если они были в оригинале как ** или <b>)
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    # Если Gemini вернул <b> в ответе, они уже экранированы как &lt;b&gt;
    text = text.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    return text

def extract_json(text):
    """Пытается вытащить JSON из текста, даже если там есть лишний мусор."""
    try:
        # Ищем всё, что похоже на {...}
        match = re.search(r'(\{.*\})', text, re.DOTALL)
        if not match:
            return None
        
        json_str = match.group(1)
        
        # Исправляем типичные ошибки ИИ:
        # 1. Заменяем «умные» кавычки
        json_str = json_str.replace('«', '"').replace('»', '"').replace('“', '"').replace('”', '"')
        # 2. Удаляем возможные запятые перед закрывающей скобкой (невалидно для JSON)
        json_str = re.sub(r',\s*\}', '}', json_str)
        
        return json.loads(json_str)
    except Exception as e:
        logger.warning(f"Failed to extract JSON from: {text[:100]}... Error: {e}")
        return None

async def process_with_gemini(title, summary, retries=2):
    """Пересказывает новость через Gemini с повторными попытками."""
    prompt = f"""Ты — экспертный игровой журналист. Перескажи новость на русском языке.
Пиши профессионально, выделяй суть. Используй <b>жирный текст</b> для фактов.
Верни ТОЛЬКО JSON без markdown оформления:
{{
  "title": "заголовок",
  "summary": "пересказанный текст с HTML тегами <b></b>",
  "tags": ["pc", "console", "mobile", "nft", "esports", "indie", "rpg", "action"] 
}}

Заголовок: {title}
Текст: {summary[:3000]}"""

    for attempt in range(retries + 1):
        try:
            response = model.generate_content(prompt)
            if not response or not response.text:
                continue
                
            data = extract_json(response.text.strip())
            
            if data:
                # Валидация полей
                return {
                    "title": str(data.get("title", title)),
                    "summary": str(data.get("summary", f"<b>{title}</b>")),
                    "tags": list(data.get("tags", []))
                }
            
            logger.info(f"Attempt {attempt+1}: Invalid JSON from Gemini, retrying...")
            await asyncio.sleep(1) # Небольшая пауза перед повтором
            
        except Exception as e:
            logger.error(f"Attempt {attempt+1}: Gemini error: {e}")
            await asyncio.sleep(1)

    # Если всё провалилось, возвращаем заглушку
    return {
        "title": title,
        "summary": f"🎮 <b>{html.escape(title)}</b>\n\nНе удалось сгенерировать пересказ.",
        "tags": []
    }
