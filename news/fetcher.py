import asyncio
import gc
import logging

import feedparser
import httpx
from bs4 import BeautifulSoup

from news.repo import get_gaming_keywords, get_rss_feeds, is_pending, is_posted

logger = logging.getLogger(__name__)


async def is_gaming(text):
    text_lower = text.lower()
    keywords = await get_gaming_keywords()
    if not keywords:
        return True
    return any(kw in text_lower for kw in keywords)


def get_image_from_entry(entry):
    if hasattr(entry, "media_content") and entry.media_content:
        return entry.media_content[0].get("url")
    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            if "image" in enc.get("type", ""):
                return enc.get("href")
    summary = entry.get("summary", "")
    if 'src="' in summary:
        start = summary.find('src="') + 5
        end = summary.find('"', start)
        return summary[start:end]
    return None


async def fetch_full_content(client, url):
    """Пытается получить полный текст статьи, если RSS-описание слишком короткое."""
    try:
        r = await client.get(url, timeout=12)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        for s in soup(["script", "style", "nav", "header", "footer", "aside", "form", "iframe"]):
            s.decompose()

        paragraphs = soup.find_all("p")
        text = "\n".join([p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 30])

        if len(text) < 100:
            content = soup.find(["article", "main", 'div[class*="content"]'])
            if content:
                text = content.get_text(separator="\n").strip()

        return text[:4000]
    except Exception as e:
        logger.debug("Scrape error for %s: %s", url, e)
        return ""


async def fetch_single_feed(client, feed_url, keywords):
    try:
        response = await client.get(feed_url, timeout=10)
        response.raise_for_status()
        feed = feedparser.parse(response.text)
        results = []
        for entry in feed.entries[:5]:
            title = entry.get("title", "")
            url = entry.get("link", "")
            summary = entry.get("summary", "")
            if not url or await is_posted(url) or await is_pending(url):
                continue

            text_to_check = (title + " " + summary).lower()
            if keywords and not any(kw in text_to_check for kw in keywords):
                continue

            if len(summary) < 400:
                full_text = await fetch_full_content(client, url)
                if full_text and len(full_text) > len(summary):
                    summary = full_text

            image_url = get_image_from_entry(entry)
            results.append({"title": title, "url": url, "summary": summary, "image_url": image_url})
        return results
    except Exception as e:
        logger.error("Feed error %s: %s", feed_url, e)
        return []


async def fetch_news():
    feeds = await get_rss_feeds()
    keywords = await get_gaming_keywords()

    if not feeds:
        logger.warning("No RSS feeds configured in database.")
        return []

    sem = asyncio.Semaphore(3)

    async def sem_fetch(client, url, keywords):
        async with sem:
            return await fetch_single_feed(client, url, keywords)

    async with httpx.AsyncClient(headers={"User-Agent": "GameBot/1.0"}, timeout=15) as client:
        tasks = [sem_fetch(client, url, keywords) for url in feeds]
        results = await asyncio.gather(*tasks)

    news = [item for sublist in results for item in sublist]
    seen_urls = set()
    unique_news = []
    for item in news:
        if item["url"] not in seen_urls:
            unique_news.append(item)
            seen_urls.add(item["url"])

    gc.collect()
    return unique_news
