import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "X-Ks-City": "750000000",
}

def get_kaspi_category_cheapest(url):
    # Пытаемся вытащить категорию из URL
    # https://kaspi.kz/shop/c/videocards/ -> videocards
    match = re.search(r'/c/([^/]+)', url)
    if not match:
        return None
    
    category = match.group(1)
    
    # Один из рабочих эндпоинтов поиска
    api_url = f"https://kaspi.kz/yml/product-view/search/filters?q=:category:{category}&text=&all=false&page=0&size=12"
    
    try:
        # Пробуем сделать запрос
        response = requests.get(api_url, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            data = response.json()
            prices = []
            for card in data.get("data", {}).get("cards", []):
                if "unitPrice" in card:
                    prices.append(card["unitPrice"])
            if prices:
                return min(prices)
        
        # Если API не сработало, попробуем еще раз с другим Referer
        HEADERS["Referer"] = f"https://kaspi.kz/shop/c/{category}/"
        response = requests.get(api_url, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            data = response.json()
            # ... (логика та же)
            prices = [c["unitPrice"] for c in data.get("data", {}).get("cards", []) if "unitPrice" in c]
            if prices: return min(prices)
            
        return None
    except:
        return None
