import json
import logging
import html
import re
import asyncio

logger = logging.getLogger(__name__)

def clean_html(raw_html):
    """Очищает текст от HTML тегов, оставляя только разрешенные Telegram (b, i, code, a)."""
    if not raw_html:
        return ""
    
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw_html, 'html.parser')
        allowed_tags = ['b', 'i', 'code', 'a']
        
        for tag in soup.find_all(True):
            if tag.name not in allowed_tags:
                tag.unwrap()
            else:
                allowed_attrs = ['href'] if tag.name == 'a' else []
                tag.attrs = {k: v for k, v in tag.attrs.items() if k in allowed_attrs}
        
        return soup.decode_contents().strip()
    except Exception as e:
        logger.error(f"Error cleaning HTML: {e}")
        return html.escape(re.sub(r'<.*?>', '', raw_html))

def extract_json(text):
    """Пытается извлечь JSON из ответа Gemini, исправляя типичные ошибки нейросетей."""
    try:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
            
        text = text.strip()
        
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            cleaned = re.sub(r'(?<=: ")(.*?)(?=",?\n|(?:"\s*}))', 
                             lambda m: m.group(1).replace('"', '\\"').replace('\n', '\\n'), 
                             text, flags=re.DOTALL)
            return json.loads(cleaned)
            
    except Exception as e:
        logger.warning(f"Failed to extract JSON. Error: {e}. Raw text: {text[:200]}...")
        return None
