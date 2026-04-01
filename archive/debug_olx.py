import requests
import re
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

url = "https://www.olx.kz/list/q-Raspberry-pi/?search%5Border%5D=created_at:desc"
response = requests.get(url, headers=HEADERS, timeout=15)
print(f"Status Code: {response.status_code}")
soup = BeautifulSoup(response.text, 'html.parser')

# Look for titles and prices
titles = soup.find_all("h6")
print(f"Found {len(titles)} h6 tags")
for t in titles[:5]:
    print(f"Title: {t.text.strip()}")

prices = soup.find_all(lambda tag: tag.name == 'p' and ('data-testid' in tag.attrs and tag.attrs['data-testid'] == 'ad-price'))
print(f"Found {len(prices)} price tags")
for p in prices[:5]:
    print(f"Price: {p.text.strip()}")

# If 0, let's look for anything with 'price' in class
if not prices:
    price_classes = soup.find_all(class_=re.compile("price", re.I))
    print(f"Found {len(price_classes)} tags with 'price' in class")
