import requests
import re
import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://kaspi.kz/",
}

def get_kaspi_category_cheapest(url):
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        
        # Kaspi часто отдает данные в JSON внутри script тега
        # Ищем паттерн: "offers": [...] или "price": ...
        
        # 1. Попробуем найти цены через регулярки в тексте страницы (JSON объекты)
        # Ищем числа рядом с "price"
        prices = re.findall(r'"price":\s*(\d+)', response.text)
        if not prices:
            # Попробуем другой паттерн, который часто встречается в их стейте
            prices = re.findall(r'"unitPrice":\s*(\d+)', response.text)
        
        if prices:
            valid_prices = [int(p) for p in prices if int(p) > 100] # Игнорируем слишком мелкие числа
            if valid_prices:
                return min(valid_prices)

        # 2. Если не нашли в JSON, пробуем BeautifulSoup с обновленными селекторами
        soup = BeautifulSoup(response.text, 'html.parser')
        price_tags = soup.find_all(class_=re.compile(r"price", re.I))
        
        found_prices = []
        for tag in price_tags:
            text = re.sub(r'[^\d]', '', tag.text)
            if text and len(text) > 2:
                found_prices.append(int(text))
        
        if found_prices:
            return min(found_prices)

        return None
    except Exception as e:
        logger.error(f"Error fetching Kaspi category {url}: {e}")
        return None
