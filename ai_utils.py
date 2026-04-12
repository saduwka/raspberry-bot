import json
import logging
import html
import re
import asyncio
import google.generativeai as genai
from config import GEMINI_API_KEY

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

genai.configure(api_key=GEMINI_API_KEY)
# Модель gemini-2.5-flash-lite (как просил пользователь, не меняем)
model = genai.GenerativeModel("gemini-2.5-flash-lite")

def clean_html(text):
    """Очищает текст от всех HTML-тегов, кроме разрешенных Telegram."""
    if not text:
        return ""
    
    # Сначала обрабатываем markdown жирный, если он пролез
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    
    # Разрешенные теги в Telegram
    allowed_tags = ['b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del', 'a', 'code', 'pre']
    
    try:
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup.find_all(True):
            if tag.name not in allowed_tags:
                tag.unwrap() # Удаляем тег, но оставляем его содержимое
            elif tag.name in ['strong', 'em', 'ins', 'strike', 'del']:
                # Маппинг синонимов в стандартные теги
                mapping = {'strong': 'b', 'em': 'i', 'ins': 'u', 'strike': 's', 'del': 's'}
                tag.name = mapping[tag.name]
        
        # Получаем очищенный HTML
        cleaned_text = str(soup)
        
        # Telegram требует, чтобы <, > и & были экранированы, если они не являются частью тегов
        # Но BeautifulSoup при выводе str(soup) обычно сам делает базовое экранирование спецсимволов внутри текста.
        return cleaned_text
    except Exception as e:
        logger.error(f"Error cleaning HTML: {e}")
        # Фолбэк на простое экранирование, если BS4 упал
        return html.escape(text)

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
    """Пересказывает новость через Gemini в стиле авторской аналитики."""
    prompt = f"""Ты — ведущий аналитик игровой индустрии. Подготовь сжатую авторскую заметку по новости на русском языке.
Стиль: серьезный, профессиональный, "человеческий" (без топорных шаблонов и формальных заголовков). 
Никаких эмодзи. Объем: до 1200 символов.

Текст должен быть связным и делиться на 2-3 логических абзаца:
1. Начни сразу с сути события, без вводных фраз. Опиши самое важное максимально прямо и профессионально.
2. Раскрой критические детали и факты, вплетая их в контекст. Используй <b>жирный текст</b> для названий игр, компаний и ключевых показателей.
3. Заверши коротким экспертным выводом: почему это важно для рынка или как это изменит пользовательский опыт в будущем.

Избегай фраз типа "Суть события", "Ключевые тезисы" и прочей бюрократии. Пиши как живой эксперт для своей аудитории.
Весь текст должен быть в поле "summary".

Верни ТОЛЬКО JSON без markdown оформления:
{{
  "title": "заголовок",
  "summary": "связный аналитический текст с HTML тегами <b></b>",
  "tags": ["pc", "ps5", "xbox", "industry", "finance", "development"] 
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
        "summary": f"🎮 <b>{clean_html(title)}</b>\n\nНе удалось сгенерировать пересказ.",
        "tags": []
    }
