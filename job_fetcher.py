import httpx
import feedparser
import logging
import asyncio
import urllib.parse
from bs4 import BeautifulSoup
from database import is_vacancy_seen, get_trade_state, get_target_companies

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}

async def fetch_with_playwright(url):
    """Открывает страницу через реальный браузер (Playwright) для обхода JS-защиты и рендеринга."""
    from playwright.async_api import async_playwright
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=HEADERS["User-Agent"])
            page = await context.new_page()
            
            # Устанавливаем таймаут и ждем загрузки сети
            await page.goto(url, wait_until="networkidle", timeout=30000)
            
            # Прокрутка вниз для динамического контента
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
            
            content = await page.content()
            await browser.close()
            return content
    except Exception as e:
        logger.error(f"Playwright error for {url}: {e}")
        return None

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

async def fetch_full_job_description(client, url):
    """Скачивает полную страницу вакансии и извлекает текст, если его нет в API/RSS."""
    try:
        r = await client.get(url, timeout=12)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        
        # Удаляем лишнее
        for s in soup(['script', 'style', 'nav', 'header', 'footer', 'aside']):
            s.decompose()
            
        # Пытаемся найти основной контент
        main_content = soup.find(['article', 'main', 'div[class*="content"]', 'div[class*="job"]', 'div[id*="job"]'])
        if main_content:
            text = main_content.get_text(separator="\n").strip()
        else:
            text = soup.get_text(separator="\n").strip()
            
        # Очищаем от множественных пробелов и переносов
        text = "\n".join([line.strip() for line in text.split("\n") if len(line.strip()) > 5])
        return text[:5000] # Лимит для Gemini
    except Exception as e:
        logger.debug(f"Description scrape error for {url}: {e}")
        return ""

async def fetch_hh_jobs(query, schedule="remote"):
    """Парсинг hh.kz через открытый API."""
    url = "https://api.hh.ru/vacancies"
    params = {
        "text": query,
        "schedule": schedule,
        "per_page": 15,
        "order_by": "publication_time"
    }
    try:
        hh_headers = HEADERS.copy()
        hh_headers["Accept"] = "application/json"
        async with httpx.AsyncClient(headers=hh_headers, timeout=12) as client:
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

async def fetch_rss_jobs(rss_url, source_name, filter_keywords=None):
    """Универсальный парсер для RSS с поддержкой фильтрации по ключевым словам и проверки на платные подписки."""
    try:
        # Включаем follow_redirects=True, чтобы не падать на 301/307 ошибках
        async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
            r = await client.get(rss_url)
            r.raise_for_status()
            feed = feedparser.parse(r.text)
            if getattr(feed, "bozo", False) and not getattr(feed, "entries", None):
                logger.warning(f"{source_name}: invalid or empty feed")
                return []
            
            jobs = []
            for entry in feed.entries[:20]:
                title = getattr(entry, "title", "")
                summary = getattr(entry, "summary", "")
                
                # Если заданы ключевые слова, проверяем заголовок и описание
                if filter_keywords:
                    text_blob = (title + " " + summary).lower()
                    if not any(kw.lower() in text_blob for kw in filter_keywords):
                        continue

                entry_url = normalize_job_url(getattr(entry, "link", None))
                if not entry_url:
                    continue
                if await is_vacancy_seen(entry_url):
                    continue
                
                jobs.append({
                    "title": title,
                    "company": entry.get("author", "N/A"),
                    "url": entry_url,
                    "salary_raw": "See website",
                    "is_remote": True,
                    "source": source_name,
                    "description": summary
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
                params={"search": query, "category": "front-end", "limit": 30},
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
    """Сканер для поиска новых ссылок на карьерных страницах компаний с использованием Playwright при необходимости."""
    all_targets = await get_target_companies()
    
    jobs = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as client:
        for company in all_targets:
            try:
                html_content = None
                # Сначала пробуем обычный быстрый запрос
                try:
                    r = await client.get(company["url"])
                    # Если 403 или пусто — используем "тяжелую артиллерию" (Playwright)
                    if r.status_code in [403, 401] or len(r.text) < 500:
                        logger.info(f"Using Playwright for {company['name']} (HTTP {r.status_code})...")
                        html_content = await fetch_with_playwright(company["url"])
                    else:
                        html_content = r.text
                except Exception as e:
                    logger.warning(f"Fast fetch failed for {company['name']}, trying Playwright: {e}")
                    html_content = await fetch_with_playwright(company["url"])
                
                if not html_content: continue
                
                soup = BeautifulSoup(html_content, 'html.parser')
                links = soup.find_all('a', href=True)
                
                for link in links:
                    text = link.get_text().strip()
                    href = link['href']
                    
                    if not text or len(text) < 5: continue
                    
                    # Простейший фильтр по ключевым словам
                    match = False
                    for kw in company["keywords"]:
                        if kw.lower() in text.lower():
                            match = True
                            break
                    
                    if match:
                        full_url = normalize_job_url(urllib.parse.urljoin(company["url"], href))
                        if not full_url or await is_vacancy_seen(full_url): continue
                        
                        jobs.append({
                            "title": text,
                            "company": company["name"],
                            "url": full_url,
                            "salary_raw": "Negotiable",
                            "is_remote": True,
                            "source": "Direct Career Page",
                            "description": f"Direct opportunity from {company['name']}"
                        })
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Error scanning {company['name']}: {e}")
    return jobs

async def fetch_all_jobs(progress_callback=None):
    """Агрегатор всех источников: локальных, прямых и международных."""
    db_query = await get_trade_state("job_search_query")
    raw_query = db_query if db_query else "Vue TypeScript Frontend"
    
    # Очистка запроса для разных API (убираем запятые, заменяем на пробелы для поиска)
    clean_query = raw_query.replace(",", " ").strip()
    hh_query = clean_query.split(" ")[0] # Для HH берем первое слово или весь если мало
    remotive_query = clean_query
    
    # Функции-фабрики для создания задач
    def get_sources(raw_query, hh_query, remotive_query):
        return [
            (lambda: fetch_hh_jobs(hh_query), 'HeadHunter'),
            (lambda: fetch_rss_jobs(f'https://career.habr.com/vacancies/rss?q={urllib.parse.quote(raw_query.replace(",", " "))}&remote=true', 'Habr Career'), 'Habr Career'),
            (lambda: fetch_rss_jobs(f'https://djinni.co/jobs/rss?primary_keyword=Frontend', 'Djinni', ['Vue', 'React', 'TypeScript', 'Frontend']), 'Djinni'),
            (lambda: fetch_remotive_jobs(remotive_query), 'Remotive'),
            (lambda: fetch_career_page_jobs(), 'Career Pages'),
            (lambda: fetch_lever_jobs(), 'Lever'),
            (lambda: fetch_greenhouse_jobs(), 'Greenhouse'),
            (lambda: fetch_ashby_jobs(), 'Ashby'),
            (lambda: fetch_rss_jobs('https://remoteok.com/remote-jobs.rss', 'RemoteOK', ['Frontend', 'React', 'Vue', 'TypeScript', 'JavaScript']), 'RemoteOK'),
            (lambda: fetch_rss_jobs('https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss', 'WeWorkRemotely'), 'WeWorkRemotely'),
            (lambda: fetch_rss_jobs('https://app.vuejobs.com/feed/posts', 'VueJobs'), 'VueJobs'),
            (lambda: fetch_rss_jobs('https://authenticjobs.com/feed/', 'AuthenticJobs', ['Frontend', 'React', 'Vue', 'TypeScript']), 'AuthenticJobs'),
            (lambda: fetch_rss_jobs('https://rss.atpchel.com/telegram/channel/frontendjobs', 'TG: Frontend Jobs', ['Vue', 'React', 'TypeScript']), 'TG: Frontend'),
            (lambda: fetch_rss_jobs('https://rss.atpchel.com/telegram/channel/javascriptjobs', 'TG: JS Jobs', ['Vue', 'React', 'TypeScript']), 'TG: JS'),
            (lambda: fetch_rss_jobs('https://rss.atpchel.com/telegram/channel/react_jobs', 'TG: React Jobs'), 'TG: React'),
            (lambda: fetch_rss_jobs('https://rss.atpchel.com/telegram/channel/remote_it_jobs', 'TG: За борт'), 'TG: Remote'),
        ]

    sources = get_sources(raw_query, hh_query, remotive_query)
    total = len(sources)
    all_results = []
    
    for i, (task_factory, name) in enumerate(sources):
        if progress_callback:
            await progress_callback(i, total, name)
        
        try:
            # Вызываем фабрику для получения корутины
            coro = task_factory()
            res = await asyncio.wait_for(coro, timeout=25) 
            all_results.append(res)
        except asyncio.TimeoutError:
            logger.error(f'Timeout fetching from {name}')
            all_results.append([])
        except Exception as e:
            logger.error(f'Error fetching from {name}: {e}')
            all_results.append([])

    if progress_callback:
        await progress_callback(total, total, "Scoring with AI...")

    unique_jobs = []
    seen_urls = set()
    for sublist in all_results:
        for job in sublist:
            normalized_url = normalize_job_url(job.get("url"))
            if not normalized_url or normalized_url in seen_urls:
                continue
            if not job.get("title") or not job.get("company"):
                continue
            
            # Базовая фильтрация по названию, чтобы отсечь явный бэкенд до AI-скоринга
            title_lower = job["title"].lower()
            exclude_keywords = [
                "backend", "devops", "qa engineer", "tester", "android", "ios", "swift", "kotlin",
                "java", "python", "php", "c++", "c#", ".net", "ruby", "rust", "go", "golang",
                "embedded", "firmware", "hardware", "data scientist", "data engineer", "ml engineer",
                "product manager", "project manager", "designer", "scrum master"
            ]
            
            # Если в названии есть исключающее слово И нет "frontend"/"front-end", пропускаем
            if any(kw in title_lower for kw in exclude_keywords):
                if "frontend" not in title_lower and "front-end" not in title_lower:
                    continue

            job["url"] = normalized_url
            unique_jobs.append(job)
            seen_urls.add(normalized_url)

    # ДОПОЛНИТЕЛЬНЫЙ ЭТАП: Догружаем описания, если они слишком короткие
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:

        # Берем только лучшие вакансии для экономии ресурсов (например, первые 20)
        for job in unique_jobs:
            desc = job.get("description", "")
            if len(desc) < 300: # Если описание подозрительно короткое
                logger.info(f"Fetching full description for {job['title']} @ {job['company']}...")
                full_desc = await fetch_full_job_description(client, job["url"])
                if full_desc:
                    job["description"] = full_desc
                await asyncio.sleep(0.5)

    return unique_jobs
