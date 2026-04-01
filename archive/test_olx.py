import price_monitor
import asyncio

url = "https://www.olx.kz/list/q-Raspberry-pi/?search%5Border%5D=created_at:desc"
price, service = price_monitor.check_price(url)
print(f"Service: {service}")
print(f"Current min price: {price}")

items = price_monitor.get_olx_category_items(url)
if items:
    print(f"Found {len(items)} items. First 3:")
    for i in items[:3]:
        print(f"- {i['name']}: {i['price']} ({i['url']})")
else:
    print("No items found.")
