import requests
import json

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "X-Ks-City": "750000000", # Алматы
    "Referer": "https://kaspi.kz/shop/",
}

def get_kaspi_category_api(category_code):
    # API эндпоинт для поиска (работает для категорий)
    api_url = f"https://kaspi.kz/yml/product-view/search/filters?page=0&all=false&q=:category:{category_code}&size=12"
    
    try:
        response = requests.get(api_url, headers=HEADERS, timeout=10)
        print(f"Status: {response.status_code}")
        data = response.json()
        
        prices = []
        if "data" in data and "cards" in data["data"]:
            for card in data["data"]["cards"]:
                if "unitPrice" in card:
                    prices.append(card["unitPrice"])
        
        return min(prices) if prices else None
    except Exception as e:
        print(f"Error: {e}")
        return None

if __name__ == "__main__":
    # Код категории можно вытащить из URL https://kaspi.kz/shop/c/videocards/ -> videocards
    p = get_kaspi_category_api("videocards")
    print(f"Result: {p}")
