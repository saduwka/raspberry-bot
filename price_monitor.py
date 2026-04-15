import httpx
import asyncio
from bs4 import BeautifulSoup
import feedparser
import re
import logging
import random
import json

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

async def _wait():
    """Rate limiting to avoid getting banned."""
    await asyncio.sleep(random.uniform(1.5, 3.5))

async def get_kaspi_price(url, client: httpx.AsyncClient):
    soup = None
    try:
        await _wait()
        response = await client.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 1. Try Meta Tags
        price_meta = soup.find("meta", property="product:price:amount")
        if price_meta and price_meta.get("content"):
            return int(float(price_meta["content"]))
        
        # 2. Try JSON-LD
        scripts = soup.find_all('script', type='application/ld+json')
        for script in scripts:
            try:
                data = json.loads(script.string)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get('@type') == 'Product' and 'offers' in item:
                        offers = item['offers']
                        if isinstance(offers, dict) and 'price' in offers:
                            return int(float(offers['price']))
                        elif isinstance(offers, list) and len(offers) > 0:
                            return int(float(offers[0].get('price', 0)))
            except:
                continue

        # 3. Fallback to common classes
        price_tag = soup.find("div", class_="item__price-once")
        if not price_tag:
            price_tag = soup.find("span", class_="offer-view__price")
            
        if price_tag:
            price_text = re.sub(r'[^\d]', '', price_tag.text)
            if price_text:
                return int(price_text)
        
        return None
    except Exception as e:
        logger.error(f"Error fetching Kaspi price from {url}: {e}")
        return None
    finally:
        if soup:
            soup.decompose()

async def get_olx_price(url, client: httpx.AsyncClient):
    soup = None
    try:
        if "/rss/" in url or url.endswith(".rss"):
            response = await client.get(url)
            feed = feedparser.parse(response.text)
            if feed.entries:
                entry = feed.entries[0]
                title = entry.get("title", "")
                price_match = re.search(r'(\d[\d\s]*)\s*(?:тг|₸|грн|\$)', title)
                if price_match:
                    return int(re.sub(r'\s', '', price_match.group(1)))
            return None
        
        await _wait()
        response = await client.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        price_tag = soup.find("div", {"data-testid": "ad-price-container"})
        if not price_tag:
            price_tag = soup.find(attrs={"data-testid": "ad-price"})

        if price_tag:
            price_text = re.sub(r'[^\d]', '', price_tag.text)
            if price_text:
                return int(price_text)
                
        return None
    except Exception as e:
        logger.error(f"Error fetching OLX price from {url}: {e}")
        return None
    finally:
        if soup:
            soup.decompose()

async def get_kaspi_category_items(url, client: httpx.AsyncClient):
    soup = None
    try:
        await _wait()
        response = await client.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        items = []

        # 1. Try JSON-LD (Search results often have ItemList)
        scripts = soup.find_all('script', type='application/ld+json')
        for script in scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get('@type') == 'ItemList':
                    for element in data.get('itemListElement', []):
                        prod = element.get('item', {})
                        if 'offers' in prod and 'price' in prod['offers']:
                            items.append({
                                "name": prod.get('name', 'Товар Kaspi'),
                                "price": int(float(prod['offers']['price'])),
                                "url": prod.get('url', url)
                            })
            except:
                continue
        
        if items: return items

        # 2. Fallback to CSS classes
        price_tags = soup.find_all("span", class_="item-card__prices-price")
        for tag in price_tags:
            price_text = re.sub(r'[^\d]', '', tag.text)
            if price_text:
                items.append({"name": "Товар из категории", "price": int(price_text), "url": url})
        
        return items
    except Exception as e:
        logger.error(f"Error fetching Kaspi category {url}: {e}")
        return []
    finally:
        if soup:
            soup.decompose()

async def get_olx_category_items(url, client: httpx.AsyncClient):
    items = []
    soup = None
    try:
        rss_url = url
        if "/rss/" not in url and not url.endswith(".rss"):
            if "olx.kz" in url:
                if "/list/" in url:
                    rss_url = url.replace("olx.kz/list/", "olx.kz/rss/list/")
                elif "/d/" in url:
                    rss_url = url.replace("olx.kz/d/", "olx.kz/rss/d/")
                else:
                    rss_url = url.replace("olx.kz/", "olx.kz/rss/")
        
        try:
            response = await client.get(rss_url)
            feed = feedparser.parse(response.text)
            if feed.entries:
                for entry in feed.entries:
                    title = entry.get("title", "")
                    link = entry.get("link", "")
                    price_match = re.search(r'(\d[\d\s]*)\s*(?:тг|₸|грн|\$)', title)
                    if price_match:
                        price = int(re.sub(r'\s', '', price_match.group(1)))
                        items.append({"name": title, "price": price, "url": link})
                if items:
                    return items
        except:
            pass

        await _wait()
        response = await client.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        cards = soup.find_all("div", {"data-cy": "l-card"})
            
        for card in cards:
            try:
                title_tag = card.find("h3") or card.find("h4") or card.find("h5") or card.find("h6")
                price_tag = card.find(attrs={"data-testid": "ad-price"})
                link_tag = card.find("a")
                
                if title_tag and price_tag and link_tag:
                    price_text = re.sub(r'[^\d]', '', price_tag.text)
                    if price_text:
                        items.append({
                            "name": title_tag.text.strip(),
                            "price": int(price_text),
                            "url": "https://www.olx.kz" + link_tag['href'] if link_tag['href'].startswith('/') else link_tag['href']
                        })
            except Exception as e:
                logger.warning(f"Failed to parse OLX card in {url}: {e}")
                continue
        return items
    except Exception as e:
        logger.error(f"Error fetching OLX items from {url}: {e}")
        return []
    finally:
        if soup:
            soup.decompose()

async def check_price(url, client: httpx.AsyncClient = None):
    # Если клиент не передан (например, вызвано вручную из bot.py), создаем временный
    if client is None:
        async with httpx.AsyncClient(headers=HEADERS, timeout=15) as temp_client:
            return await _check_price_internal(url, temp_client)
    return await _check_price_internal(url, client)

async def _check_price_internal(url, client: httpx.AsyncClient):
    if "kaspi.kz" in url:
        if "/c/" in url:
            items = await get_kaspi_category_items(url, client)
            price = min(i['price'] for i in items) if items else None
            return price, "Kaspi (Категория)", items
        return await get_kaspi_price(url, client), "Kaspi", None
    elif "olx.kz" in url or "olx.ua" in url:
        if "/rss/" in url or "search" in url or "/q-" in url or not url.endswith(".html"):
            items = await get_olx_category_items(url, client)
            price = min(i['price'] for i in items) if items else None
            return price, "OLX (Категория)", items
        return await get_olx_price(url, client), "OLX", None
    return None, None, None
