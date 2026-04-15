import httpx
import feedparser
import logging
import asyncio
import urllib.parse
from bs4 import BeautifulSoup
from database import is_vacancy_seen, get_trade_state

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "JobHuntBot/1.0"}

# Список целевых международных компаний для прямого мониторинга
TARGET_COMPANIES = [
    {"name": "GitLab", "url": "https://about.gitlab.com/jobs/all-jobs/", "keywords": ["Engineer", "Frontend", "Vue", "Fullstack"]},
    {"name": "Revolut", "url": "https://www.revolut.com/careers/search-jobs/", "keywords": ["Engineer", "Backend", "Frontend"]},
    {"name": "Doist", "url": "https://doist.com/careers/", "keywords": ["Engineer", "Product"]},
    {"name": "Canonical", "url": "https://canonical.com/careers/all-vacancies", "keywords": ["Engineer", "Python", "Go"]},
    {"name": "JetBrains", "url": "https://www.jetbrains.com/careers/jobs/", "keywords": ["Engineer", "Frontend"]},
    {"name": "Elastic", "url": "https://www.elastic.co/about/careers", "keywords": ["Engineer"]},
    {"name": "Grafana Labs", "url": "https://grafana.com/about/careers/open-positions/", "keywords": ["Engineer"]},
]

LEVER_COMPANIES = [
    {"name": "Plaid", "board": "plaid"},
]

GREENHOUSE_COMPANIES = [
    {"name": "Vercel", "board": "vercel"},
]

ASHBY_COMPANIES = [
    {"name": "Linear", "board": "linear"},
    {"name": "Render", "board": "render"},
]

def normalize_job_url(url):
    if not url:
        return None
    return url.split("#")[0].rstrip("/")

def looks_remote(text):
    if not text:
        return False
    text_lower = text.lower()
    remote_markers = [
        "remote", "worldwide", "distributed", "work from anywhere",
        "remote-first", "home-based",
    ]
    return any(marker in text_lower for marker in remote_markers)

async def fetch_hh_jobs(query, schedule="remote"):
    """Парсинг hh.kz через открытый API."""
    url = "https://api.hh.ru/vacancies"
    params = {
        "text": query,
        "schedule": schedule,
        "per_page": 10,
        "order_by": "publication_time"
    }
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=10) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            
            jobs = []
            for item in data.get("items", []):
                v_url = normalize_job_url(item.get("alternate_url"))
                if not v_url:
                    continue
                if await is_vacancy_seen(v_url): continue
                
                salary = item.get("salary")
                salary_text = "Не указана"
                if salary:
                    f = salary.get("from")
                    t = salary.get("to")
                    cur = salary.get("currency")
                    salary_text = f"{f or ''} - {t or ''} {cur}"

                jobs.append({
                    "title": item.get("name"),
                    "company": item.get("employer", {}).get("name"),
                    "url": v_url,
                    "salary_raw": salary_text,
                    "is_remote": True,
                    "source": "hh.kz",
                    "description": " ".join(filter(None, [
                        item.get("snippet", {}).get("requirement"),
                        item.get("snippet", {}).get("responsibility"),
                    ]))
                })
            return jobs
    except Exception as e:
        logger.error(f"HH Error: {e}")
        return []

async def fetch_rss_jobs(rss_url, source_name):
    """Универсальный парсер для RSS (LinkedIn, Habr, WWR)."""
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
            r = await client.get(rss_url)
            r.raise_for_status()
            feed = feedparser.parse(r.text)
            if getattr(feed, "bozo", False) and not getattr(feed, "entries", None):
                logger.warning(f"{source_name}: invalid or empty feed")
                return []
            
            jobs = []
            for entry in feed.entries[:15]:
                entry_url = normalize_job_url(getattr(entry, "link", None))
                if not entry_url:
                    continue
                if await is_vacancy_seen(entry_url):
                    continue
                
                jobs.append({
                    "title": entry.title,
                    "company": entry.get("author", "N/A"),
                    "url": entry_url,
                    "salary_raw": "See website",
                    "is_remote": True,
                    "source": source_name,
                    "description": entry.get("summary", "")
                })
            return jobs
    except Exception as e:
        logger.error(f"{source_name} Error: {e}")
        return []

async def fetch_remotive_jobs(query):
    """Публичный API Remotive. Просить слишком часто не нужно."""
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=20) as client:
            r = await client.get(
                "https://remotive.com/api/remote-jobs",
                params={"search": query, "category": "software-dev", "limit": 30},
            )
            r.raise_for_status()
            data = r.json()

            jobs = []
            for item in data.get("jobs", []):
                url = normalize_job_url(item.get("url"))
                if not url or await is_vacancy_seen(url):
                    continue

                jobs.append({
                    "title": item.get("title"),
                    "company": item.get("company_name"),
                    "url": url,
                    "salary_raw": item.get("salary") or "See website",
                    "is_remote": True,
                    "source": "Remotive API",
                    "description": item.get("description", ""),
                })
            return jobs
    except Exception as e:
        logger.error(f"Remotive API Error: {e}")
        return []

async def fetch_lever_jobs():
    jobs = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=20) as client:
        for company in LEVER_COMPANIES:
            try:
                r = await client.get(f"https://api.lever.co/v0/postings/{company['board']}?mode=json")
                r.raise_for_status()
                data = r.json()

                for item in data:
                    text_blob = " ".join(filter(None, [
                        item.get("text"),
                        item.get("descriptionPlain"),
                        item.get("categories", {}).get("team"),
                        item.get("categories", {}).get("location"),
                    ]))
                    if not looks_remote(text_blob):
                        continue

                    url = normalize_job_url(item.get("hostedUrl"))
                    if not url or await is_vacancy_seen(url):
                        continue

                    jobs.append({
                        "title": item.get("text"),
                        "company": company["name"],
                        "url": url,
                        "salary_raw": "See website",
                        "is_remote": True,
                        "source": f"Lever:{company['name']}",
                        "description": item.get("descriptionPlain") or item.get("description") or "",
                    })
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Lever Error [{company['name']}]: {e}")
    return jobs

async def fetch_greenhouse_jobs():
    jobs = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=20) as client:
        for company in GREENHOUSE_COMPANIES:
            try:
                r = await client.get(
                    f"https://boards-api.greenhouse.io/v1/boards/{company['board']}/jobs",
                    params={"content": "true"},
                )
                r.raise_for_status()
                data = r.json()

                for item in data.get("jobs", []):
                    location = (item.get("location") or {}).get("name", "")
                    content = item.get("content", "") or ""
                    text_blob = " ".join(filter(None, [item.get("title"), location, content]))
                    if not looks_remote(text_blob):
                        continue

                    absolute_url = normalize_job_url(item.get("absolute_url"))
                    if not absolute_url or await is_vacancy_seen(absolute_url):
                        continue

                    jobs.append({
                        "title": item.get("title"),
                        "company": company["name"],
                        "url": absolute_url,
                        "salary_raw": "See website",
                        "is_remote": True,
                        "source": f"Greenhouse:{company['name']}",
                        "description": content,
                    })
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Greenhouse Error [{company['name']}]: {e}")
    return jobs

async def fetch_ashby_jobs():
    jobs = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        for company in ASHBY_COMPANIES:
            try:
                r = await client.get(f"https://jobs.ashbyhq.com/{company['board']}")
                r.raise_for_status()
                soup = BeautifulSoup(r.text, 'html.parser')
                links = soup.find_all('a', href=True)

                for link in links:
                    href = urllib.parse.urljoin(f"https://jobs.ashbyhq.com/{company['board']}", link["href"])
                    href = normalize_job_url(href)
                    text = " ".join(link.stripped_strings)
                    if not href or f"/{company['board']}/" not in href:
                        continue
                    if not text or not looks_remote(text):
                        continue
                    if await is_vacancy_seen(href):
                        continue

                    jobs.append({
                        "title": text.split("@")[0].strip(),
                        "company": company["name"],
                        "url": href,
                        "salary_raw": "See website",
                        "is_remote": True,
                        "source": f"Ashby:{company['name']}",
                        "description": text,
                    })
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Ashby Error [{company['name']}]: {e}")
    return jobs

async def fetch_career_page_jobs():
    """Сканер для поиска новых ссылок на карьерных страницах компаний."""
    jobs = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as client:
        for company in TARGET_COMPANIES:
            try:
                r = await client.get(company["url"])
                if r.status_code != 200: continue
                
                soup = BeautifulSoup(r.text, 'html.parser')
                links = soup.find_all('a', href=True)
                
                for link in links:
                    text = link.get_text().strip()
                    href = link['href']
                    
                    # Простейший фильтр по ключевым словам
                    if any(kw.lower() in text.lower() for kw in company["keywords"]):
                        full_url = normalize_job_url(urllib.parse.urljoin(company["url"], href))
                        if not full_url:
                            continue
                        
                        if await is_vacancy_seen(full_url): continue
                        
                        jobs.append({
                            "title": text,
                            "company": company["name"],
                            "url": full_url,
                            "salary_raw": "Negotiable",
                            "is_remote": True,
                            "source": "Direct Career Page",
                            "description": f"Direct opportunity from {company['name']}"
                        })
                await asyncio.sleep(1) # Небольшая пауза между компаниями
            except Exception as e:
                logger.error(f"Error scanning {company['name']}: {e}")
    return jobs

async def fetch_all_jobs():
    """Агрегатор всех источников: локальных, прямых и международных."""
    db_query = await get_trade_state("job_search_query")
    query = db_query if db_query else "Vue TypeScript Frontend"
    
    encoded_query = urllib.parse.quote(query)
    
    tasks = [
        # Локальные / СНГ
        fetch_hh_jobs(query),
        fetch_rss_jobs(f"https://career.habr.com/vacancies/rss?q={encoded_query}&remote=true", "Habr Career"),
        fetch_remotive_jobs(query),
        
        # Прямой мониторинг
        fetch_career_page_jobs(),
        fetch_lever_jobs(),
        fetch_greenhouse_jobs(),
        fetch_ashby_jobs(),
        
        # Глобальные (Remote-first)
        fetch_rss_jobs("https://weworkremotely.com/categories/remote-programming-jobs.rss", "WeWorkRemotely"),
        fetch_rss_jobs("https://remoteok.com/remote-jobs.rss", "Remote OK"),
    ]
    results = await asyncio.gather(*tasks)
    unique_jobs = []
    seen_urls = set()
    for sublist in results:
        for job in sublist:
            normalized_url = normalize_job_url(job.get("url"))
            if not normalized_url or normalized_url in seen_urls:
                continue
            if not job.get("title") or not job.get("company"):
                continue
            job["url"] = normalized_url
            unique_jobs.append(job)
            seen_urls.add(normalized_url)

    return unique_jobs
