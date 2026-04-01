import sqlite3
import re

DB_PATH = "bot.db"

def clean_name(url, service):
    # Упрощенная логика очистки для существующих записей
    clean_url = url.split("?")[0].rstrip("/")
    slug = clean_url.split("/")[-1].replace(".html", "").replace("-", " ")
    
    if "search" in url or "list" in url or "rss" in url or "/c/" in url or not url.endswith(".html"):
        query_match = re.search(r'q-([^/?&]+)', url) or re.search(r'q=([^&]+)', url)
        if query_match:
            return f"Поиск: {query_match.group(1).replace('+', ' ').replace('%20', ' ')}"
        return f"Кат: {slug.capitalize()}"
    return slug.capitalize()

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute("SELECT id, url, service FROM watches")
rows = c.fetchall()

for row_id, url, service in rows:
    new_name = clean_name(url, service)
    c.execute("UPDATE watches SET name=? WHERE id=?", (new_name, row_id))
    print(f"ID {row_id}: Updated name to '{new_name}'")

conn.commit()
conn.close()
print("Done cleaning database.")
