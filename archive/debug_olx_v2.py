import requests
import re
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

url = "https://www.olx.kz/list/q-Raspberry-pi/?search%5Border%5D=created_at:desc"
response = requests.get(url, headers=HEADERS, timeout=15)
soup = BeautifulSoup(response.text, 'html.parser')

cards = soup.find_all("div", {"data-cy": "l-card"})
print(f"Found {len(cards)} cards")

for card in cards[:3]:
    # Look for title inside card
    title = card.find("h3") or card.find("h4") or card.find("h5") or card.find("h6") or card.find("span")
    price = card.find("p", {"data-testid": "ad-price"})
    link = card.find("a")
    print(f"Card: Title={title.text[:30] if title else 'N/A'}, Price={price.text if price else 'N/A'}, Link={link['href'] if link else 'N/A'}")
