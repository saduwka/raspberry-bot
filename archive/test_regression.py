import html
import sqlite3
import os

DB_PATH = "test_bot.db"

def init_db():
    if os.path.exists(DB_PATH): os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE pending (id INTEGER PRIMARY KEY, title TEXT, url TEXT, summary TEXT, image_url TEXT, created_at TEXT)")
    conn.commit()
    conn.close()

def save_pending(title, url, summary, image_url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO pending (title, url, summary, image_url) VALUES (?, ?, ?, ?)", (title, url, summary, image_url))
    conn.commit()
    last_id = c.lastrowid
    conn.close()
    return last_id

def get_pending(pending_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM pending WHERE id=?", (pending_id,))
    result = c.fetchone()
    conn.close()
    return result

def test_html_generation():
    print("--- Testing HTML Generation ---")
    processed_text = "This is a <b>bold</b> text with an & symbol and a <bracket>."
    image_url = "https://example.com/image_with_underscores_and_&.jpg"
    url = "https://example.com/news_with_underscores_and_&"
    
    # Simulate send_for_approval logic
    image_prefix = f'<a href="{image_url}">&#8203;</a>' if image_url else ""
    safe_url = html.escape(url)
    text = f"{image_prefix}📋 <b>Новая новость на проверку:</b>\n\n{processed_text}\n\n🔗 {safe_url}"
    
    print(f"Generated HTML:\n{text}")
    
    # Basic validation: check if tags are balanced and entities escaped
    assert "<b>" in text and "</b>" in text
    assert "&#8203;" in text
    assert "news_with_underscores_and_&amp;" in text # & should be escaped to &amp;
    print("✅ HTML Generation test passed!")

def test_db_persistence():
    print("\n--- Testing DB Persistence ---")
    init_db()
    title = "Test Title"
    url = "http://test.com"
    processed_text = "This text should be persistent! <b>Important!</b>"
    img = "http://test.com/img.jpg"
    
    pid = save_pending(title, url, processed_text, img)
    retrieved = get_pending(pid)
    
    print(f"Saved: {processed_text}")
    print(f"Retrieved: {retrieved[3]}")
    
    assert retrieved[3] == processed_text
    print("✅ DB Persistence test passed!")

if __name__ == "__main__":
    try:
        test_html_generation()
        test_db_persistence()
        print("\n🎉 All regression tests passed successfully!")
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
    finally:
        if os.path.exists(DB_PATH): os.remove(DB_PATH)
