import asyncio
import price_monitor
import logging

logging.basicConfig(level=logging.INFO)

async def main():
    url = 'https://www.olx.kz/elektronika/igry-i-igrovye-pristavki/'
    print(f"Testing URL: {url}")
    price, service, items = await price_monitor.check_price(url)
    print(f"Price: {price}")
    print(f"Service: {service}")
    print(f"Items count: {len(items) if items else 0}")
    if items:
        print(f"First item: {items[0]['name']} - {items[0]['price']}")

if __name__ == "__main__":
    asyncio.run(main())
