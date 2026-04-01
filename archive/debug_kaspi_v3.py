import requests
import re
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1"
}

def get_kaspi_category_cheapest(url):
    session = requests.Session()
    try:
        # Сначала "заходим" на главную, чтобы получить куки
        session.get("https://kaspi.kz/", headers=HEADERS, timeout=10)
        
        # Теперь на страницу категории
        response = session.get(url, headers=HEADERS, timeout=15)
        
        # Проверим, не забанили ли нас (Kaspi отдает 403 или капчу)
        if "captcha" in response.text.lower():
            print("CAPTCHA DETECTED")
            return None
        
        # Ищем цены в тексте (регуляркой по JSON объектам)
        # Kaspi часто хранит их в unitPrice или просто price внутри скриптов
        prices = re.findall(r'"price":\s*(\d+)', response.text)
        if not prices:
            prices = re.findall(r'unitPrice":\s*(\d+)', response.text)
            
        if prices:
            valid_prices = [int(p) for p in prices if int(p) > 500]
            if valid_prices:
                return min(valid_prices)
        
        # Запасной вариант - BeautifulSoup
        soup = BeautifulSoup(response.text, 'html.parser')
        # Ищем все span или div, содержащие "тг" или "₸"
        for tag in soup.find_all(["span", "div"], string=re.compile(r"тг|₸")):
            text = re.sub(r'[^\d]', '', tag.text)
            if text and len(text) > 3:
                return int(text) # Возвращаем первую найденную цену (обычно они в списке)

        return None
    except Exception as e:
        print(f"Error: {e}")
        return None

if __name__ == "__main__":
    p = get_kaspi_category_cheapest("https://kaspi.kz/shop/c/videocards/")
    print(f"Result: {p}")
