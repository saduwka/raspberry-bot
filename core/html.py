import html
import logging
import re

logger = logging.getLogger(__name__)


def clean_html(raw_html):
    """Очищает текст от HTML тегов, оставляя только разрешенные Telegram (b, i, code, a)."""
    if not raw_html:
        return ""

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw_html, "html.parser")
        allowed_tags = ["b", "i", "code", "a"]

        for tag in soup.find_all(True):
            if tag.name not in allowed_tags:
                tag.unwrap()
            else:
                allowed_attrs = ["href"] if tag.name == "a" else []
                tag.attrs = {k: v for k, v in tag.attrs.items() if k in allowed_attrs}

        result = soup.decode_contents().strip()
        logger.debug("Cleaned HTML: %s", result)
        return result
    except Exception as e:
        logger.error("Error cleaning HTML: %s", e)
        fallback = html.escape(re.sub(r"<.*?>", "", raw_html))
        logger.debug("Fallback cleaned HTML: %s", fallback)
        return fallback


def validate_html(html_text):
    """Проверяет корректность HTML-тегов."""
    if not html_text:
        return False, "Пустой HTML"

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text, "html.parser")
        
        # Проверка на недопустимые теги
        for tag in soup.find_all(True):
            if tag.name not in ["b", "i", "code", "a"]:
                return False, f"Недопустимый тег: {tag.name}"
        
        # Проверка на недопустимые атрибуты
        for tag in soup.find_all(True):
            for attr in tag.attrs:
                if attr not in ["href"]:
                    return False, f"Недопустимый атрибут: {attr}"
        
        # Проверка на закрытие тегов
        open_tags = []
        for tag in soup.find_all(True):
            if tag.name not in ["b", "i", "code", "a"]:
                continue
            if tag.name in open_tags:
                return False, f"Незакрытый тег: {tag.name}"
            open_tags.append(tag.name)
        
        return True, "OK"
    except Exception as e:
        return False, f"Ошибка валидации: {e}"


def ensure_valid_html(html_text, fallback_text=None):
    """Обеспечивает корректный HTML, возвращает чистый текст или fallback."""
    if not html_text:
        return fallback_text if fallback_text else ""
    
    valid, msg = validate_html(html_text)
    if not valid:
        logger.warning("Invalid HTML detected: %s, using fallback", msg)
        # Убираем все HTML-теги, оставляем только текст
        clean = re.sub(r"<[^>]+>", "", html_text)
        return clean.strip() if clean.strip() else fallback_text if fallback_text else ""
    
    return html_text.strip()
