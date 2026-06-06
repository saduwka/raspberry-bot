import httpx
import feedparser
import logging
import asyncio
import urllib.parse
from bs4 import BeautifulSoup
from database import is_vacancy_seen, get_trade_state, get_target_companies
from ai.jobs import expand_search_query

logger = logging.getLogger(__name__)

# Singleton-like manager for Playwright
class PlaywrightManager:
    _instance = None
    _browser = None
    _playwright = None

    @classmethod
    async def get_browser(cls):
        if cls._browser is None:
            from playwright.async_api import async_playwright
            cls._playwright = await async_playwright().start()
            # Оптимизированные аргументы для запуска Chromium на Raspberry Pi (минимум процессов и ОЗУ)
            cls._browser = await cls._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-zygote",
                    "--single-process",
                    "--js-flags=--max-old-space-size=256"
                ]
            )
        return cls._browser

    @classmethod
    async def close(cls):
        if cls._browser:
            await cls._browser.close()
            cls._browser = None
        if cls._playwright:
            await cls._playwright.stop()
            cls._playwright = None


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# Блокировка для гарантированно последовательного использования браузера
browser_lock = asyncio.Lock()

async def fetch_with_playwright(url):
    """Открывает страницу через реальный браузер (Playwright) для обхода JS-защиты и рендеринга."""
    async with browser_lock:
        try:
            browser = await PlaywrightManager.get_browser()
            context = await browser.new_context(user_agent=HEADERS["User-Agent"])
            page = await context.new_page()
            
            try:
                # Устанавливаем таймаут и ждем загрузки сети
                await page.goto(url, wait_until="networkidle", timeout=25000)
                
                # Прокрутка вниз для динамического контента
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1.5)
                
                content = await page.content()
                return content
            finally:
                await context.close()
        except Exception as e:
            logger.debug(f"Playwright error for {url}: {e}")
            return None


LEVER_COMPANIES = [
    {"name": "Plaid", "board": "plaid"},
    {"name": "Postscript", "board": "postscript"},
    {"name": "Hotjar", "board": "hotjar"},
    {"name": "Miro", "board": "miro"},
    {"name": "Figma", "board": "figma"},
    {"name": "Supabase", "board": "supabase"},
    {"name": "Docker", "board": "docker"},
    {"name": "Sourcegraph", "board": "sourcegraph"},
    {"name": "Vanta", "board": "vanta"},
    {"name": "OpenAI", "board": "openai"},
    {"name": "Loom", "board": "loom"},
]

GREENHOUSE_COMPANIES = [
    {"name": "Vercel", "board": "vercel"},
    {"name": "GitLab", "board": "gitlab"},
    {"name": "Stripe", "board": "stripe"},
    {"name": "Airbnb", "board": "airbnb"},
    {"name": "DoorDash", "board": "doordash"},
    {"name": "Affirm", "board": "affirm"},
    {"name": "Sentry", "board": "sentry"},
    {"name": "Okta", "board": "okta"},
    {"name": "Retool", "board": "retool"},
    {"name": "Grafana", "board": "grafana"},
    {"name": "HashiCorp", "board": "hashicorp"},
]

ASHBY_COMPANIES = [
    {"name": "Linear", "board": "linear"},
    {"name": "Render", "board": "render"},
    {"name": "Oyster", "board": "oyster"},
    {"name": "Replicate", "board": "replicate"},
    {"name": "Stability AI", "board": "stabilityai"},
    {"name": "Railway", "board": "railway"},
    {"name": "Cursor", "board": "anysphere"},
    {"name": "Perplexity", "board": "perplexity"},
    {"name": "Deel", "board": "deel"},
    {"name": "Notion", "board": "notion"},
]

STATIC_RSS_SOURCES = [
    # (url, source_name, filter_keywords)
    ("https://djinni.co/jobs/rss?primary_keyword=Frontend",  "Djinni",         ["Vue", "React", "TypeScript", "Frontend"]),
    ("https://himalayas.app/jobs.rss",                       "Himalayas",       ["Frontend", "Vue", "React", "TypeScript"]),
    ("https://jsremotely.com/jobs/rss",                      "JS Remotely",     None),
    ("https://remoteok.com/remote-jobs.rss",                 "RemoteOK",        ["Frontend", "React", "Vue", "TypeScript", "JavaScript"]),
    ("https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss", "WeWorkRemotely", None),
    ("https://app.vuejobs.com/feed/posts",                   "VueJobs",         None),
    ("https://authenticjobs.com/feed/",                      "AuthenticJobs",   ["Frontend", "React", "Vue", "TypeScript"]),
    ("https://web3.career/remote-jobs.rss",                  "Web3.career",     ["Frontend", "React", "Vue", "TypeScript", "JavaScript"]),
    ("https://nodesk.co/remote-jobs/index.xml",              "NoDesk",          ["Frontend", "React", "Vue", "TypeScript", "JavaScript"]),
    ("https://jobspresso.co/feed/?post_type=job_listing",    "Jobspresso",      ["Frontend", "React", "Vue", "TypeScript", "JavaScript"]),
    ("https://jobs.jsconf.org/jobs.xml",                     "JSConf Jobs",     ["Frontend", "React", "Vue", "TypeScript", "JavaScript"]),
]


def _build_vacancy(
    title: str,
    company: str,
    url: str,
    source: str,
    description: str = "",
    salary: str = "See website",
    is_remote: bool = True,
) -> dict:
    """Стандартный конструктор словаря вакансии. Используйте вместо ручного создания дикта во всех fetch-функциях."""
    return {
        "title":       title,
        "company":     company,
        "url":         url,
        "salary_raw":  salary,
        "is_remote":   is_remote,
        "source":      source,
        "description": description,
    }


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
    """Парсинг hh.ru через открытый API."""
    url = "https://api.hh.ru/vacancies"
    params = {
        "text": query,
        "schedule": schedule,
        "per_page": 20,
        "order_by": "publication_time"
    }
    try:
        hh_headers = HEADERS.copy()
        # HH API требует уникальный User-Agent.
        hh_headers["User-Agent"] = f"JobHunterBot/1.0 (contact@nurzhan.me)"
        hh_headers["Accept"] = "application/json"
        async with httpx.AsyncClient(headers=hh_headers, timeout=15) as client:
            r = await client.get(url, params=params)
            if r.status_code == 403:
                logger.warning("HH API returned 403. Access might be restricted by IP.")
                return []
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

                jobs.append(_build_vacancy(
                    title=item.get("name"),
                    company=item.get("employer", {}).get("name"),
                    url=v_url,
                    source="hh.kz",
                    description=" ".join(filter(None, [
                        item.get("snippet", {}).get("requirement"),
                        item.get("snippet", {}).get("responsibility"),
                    ])),
                    salary=salary_text,
                ))
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
                
                jobs.append(_build_vacancy(
                    title=title,
                    company=entry.get("author", "N/A"),
                    url=entry_url,
                    source=source_name,
                    description=summary,
                ))
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

                jobs.append(_build_vacancy(
                    title=item.get("title"),
                    company=item.get("company_name"),
                    url=url,
                    source="Remotive API",
                    description=item.get("description", ""),
                    salary=item.get("salary") or "See website",
                ))
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

                    jobs.append(_build_vacancy(
                        title=item.get("text"),
                        company=company["name"],
                        url=url,
                        source=f"Lever:{company['name']}",
                        description=item.get("descriptionPlain") or item.get("description") or "",
                    ))
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

                    jobs.append(_build_vacancy(
                        title=item.get("title"),
                        company=company["name"],
                        url=absolute_url,
                        source=f"Greenhouse:{company['name']}",
                        description=content,
                    ))
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

                    jobs.append(_build_vacancy(
                        title=text.split("@")[0].strip(),
                        company=company["name"],
                        url=href,
                        source=f"Ashby:{company['name']}",
                        description=text,
                    ))
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
                        
                        jobs.append(_build_vacancy(
                            title=text,
                            company=company["name"],
                            url=full_url,
                            source="Direct Career Page",
                            description=f"Direct opportunity from {company['name']}",
                            salary="Negotiable",
                        ))
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Error scanning {company['name']}: {e}")
    return jobs

async def fetch_remote_co_jobs(query):
    """Скрапер Remote.co с быстрым HTTP-запросом и переходом на Playwright в случае ошибки/блокировки."""
    url = "https://remote.co/remote-jobs/developer"
    html = None
    try:
        # Сначала пробуем быстрый HTTP-запрос
        async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code == 200 and len(r.text) > 1000:
                html = r.text
                logger.info("Successfully fetched Remote.co via fast HTTP.")
    except Exception as e:
        logger.warning(f"Fast HTTP fetch failed for Remote.co, trying Playwright fallback: {e}")

    if not html:
        logger.info("Falling back to Playwright for Remote.co...")
        html = await fetch_with_playwright(url)

    if not html:
        return []

    try:
        soup = BeautifulSoup(html, 'html.parser')
        job_list = soup.find_all('a', class_='card')
        
        jobs = []
        for card in job_list[:25]:
            title_tag = card.find('span', class_='font-weight-bold')
            if not title_tag: continue
            
            title = title_tag.get_text().strip()
            v_url = normalize_job_url(urllib.parse.urljoin("https://remote.co", card['href']))
            if await is_vacancy_seen(v_url): continue
            
            jobs.append(_build_vacancy(
                title=title,
                company="Remote.co",
                url=v_url,
                source="Remote.co",
                description=title,
            ))
        return jobs
    except Exception as e:
        logger.error(f"Remote.co Scrape Error: {e}")
        return []


async def fetch_hn_jobs():
    """Hacker News 'Who is hiring': Парсинг через Algolia API."""
    try:
        # 1. Ищем последний тред "Who is hiring"
        search_url = "https://hn.algolia.com/api/v1/search_by_date"
        params = {
            "query": "Who is hiring",
            "tags": "story,author_whoishiring",
            "hitsPerPage": 1
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(search_url, params=params)
            r.raise_for_status()
            hits = r.json().get("hits", [])
            if not hits: return []
            
            story_id = hits[0]["objectID"]
            story_title = hits[0]["title"]
            
            # 2. Получаем комментарии (вакансии)
            item_url = f"https://hn.algolia.com/api/v1/items/{story_id}"
            r = await client.get(item_url)
            r.raise_for_status()
            comments = r.json().get("children", [])
            
            jobs = []
            for comment in comments[:100]: # Берем первые 100 для анализа
                text = comment.get("text", "")
                if not text: continue
                
                # HN вакансии обычно начинаются с Названия Компании | Роли | Локации
                # Упрощенно проверяем на Remote и ключевые слова
                text_plain = BeautifulSoup(text, 'html.parser').get_text(separator=" ")
                if not looks_remote(text_plain): continue
                
                # Фильтр по стеку (Vue/React/Frontend)
                keywords = ["vue", "react", "frontend", "front-end", "typescript"]
                if not any(kw in text_plain.lower() for kw in keywords): continue
                
                v_url = f"https://news.ycombinator.com/item?id={comment['id']}"
                if await is_vacancy_seen(v_url): continue
                
                # Пытаемся вычленить заголовок (первая строка до пайпа или переноса)
                first_line = text_plain.split("\n")[0].split("|")[0].strip()
                
                jobs.append(_build_vacancy(
                    title=first_line[:100],
                    company="HN Startup",
                    url=v_url,
                    source=f"Hacker News ({story_title})",
                    description=text_plain,
                    salary="See HN thread",
                ))
            return jobs
    except Exception as e:
        logger.error(f"HN Error: {e}")
        return []

async def fetch_all_jobs(progress_callback=None):
    """Агрегатор всех источников с использованием ИИ-расширения запросов (Параллельно)."""
    db_query = await get_trade_state("job_search_query")
    raw_query = db_query if db_query else "Vue TypeScript Frontend"
    
    # 1. Расширяем запрос через ИИ
    if progress_callback:
        await progress_callback(0, 100, "Expanding search queries via Gemini...")
    
    query_variations = await expand_search_query(raw_query)
    
    # Функции-фабрики для создания задач
    def get_sources(queries):
        # Статические источники: Playwright/API-борды и RSS из константы STATIC_RSS_SOURCES
        sources = [
            (lambda: fetch_career_page_jobs(), 'Career Pages'),
            (lambda: fetch_lever_jobs(),        'Lever'),
            (lambda: fetch_greenhouse_jobs(),   'Greenhouse'),
            (lambda: fetch_ashby_jobs(),        'Ashby'),
            (lambda: fetch_hn_jobs(),           'Hacker News'),
            (lambda: fetch_remote_co_jobs(queries[0]), 'Remote.co'),
        ] + [
            (lambda u=url, n=name, k=kw: fetch_rss_jobs(u, n, k), name)
            for url, name, kw in STATIC_RSS_SOURCES
        ]

        # Динамические мульти-запросы для ключевых платформ
        for q in queries[:5]:  # Топ-5 вариаций
            q_clean = q.replace(",", " ").strip()
            hh_q = q_clean.split(" ")[0]
            sources.append((lambda q=hh_q: fetch_hh_jobs(q),       f'HH:{q}'))
            sources.append((lambda q=q_clean: fetch_remotive_jobs(q), f'Remotive:{q}'))
            sources.append((lambda q=q_clean: fetch_rss_jobs(
                f'https://career.habr.com/vacancies/rss?q={urllib.parse.quote(q_clean)}&remote=true',
                f'Habr:{q}'
            ), f'Habr:{q}'))

        return sources

    sources = get_sources(query_variations)
    total = len(sources)
    all_results = []
    
    sem = asyncio.Semaphore(6)

    async def wrapped_task(task_factory, name, index):
        async with sem:
            if progress_callback:
                await progress_callback(index, total, name)
            try:
                coro = task_factory()
                return await asyncio.wait_for(coro, timeout=35)
            except asyncio.TimeoutError:
                logger.error(f'Timeout fetching from {name}')
                return []
            except Exception as e:
                logger.error(f'Error fetching from {name}: {e}')
                return []

    try:
        tasks = [wrapped_task(tf, name, i) for i, (tf, name) in enumerate(sources)]
        all_results = await asyncio.gather(*tasks)
    finally:
        await PlaywrightManager.close()

    if progress_callback:
        await progress_callback(total, total, "Filtering & Deduplicating...")

    unique_jobs = []
    seen_urls = set()
    seen_identifiers = set() # For company + title deduplication
    
    for sublist in all_results:
        for job in sublist:
            normalized_url = normalize_job_url(job.get("url"))
            title = job.get("title", "").strip()
            company = job.get("company", "").strip()
            
            if not normalized_url or not title or not company:
                continue
                
            # 1. URL Deduplication
            if normalized_url in seen_urls:
                continue
            
            # 2. Company + Title Deduplication
            clean_title = "".join(filter(str.isalnum, title.lower()))
            clean_company = "".join(filter(str.isalnum, company.lower()))
            identifier = f"{clean_company}_{clean_title}"
            
            if identifier in seen_identifiers:
                continue

            # 3. Basic Title Filtering
            title_lower = title.lower()
            exclude_keywords = [
                "backend", "devops", "qa engineer", "tester", "android", "ios", "swift", "kotlin",
                "java", "python", "php", "c++", "c#", ".net", "ruby", "rust", "go", "golang",
                "embedded", "firmware", "hardware", "data scientist", "data engineer", "ml engineer",
                "product manager", "project manager", "designer", "scrum master"
            ]
            
            if any(kw in title_lower for kw in exclude_keywords):
                if "frontend" not in title_lower and "front-end" not in title_lower:
                    continue

            # Passed all filters
            job["url"] = normalized_url
            unique_jobs.append(job)
            seen_urls.add(normalized_url)
            seen_identifiers.add(identifier)

    if progress_callback:
        await progress_callback(total, total, "Enriching descriptions...")

    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        enrich_sem = asyncio.Semaphore(5)

        async def enrich_job(job):
            async with enrich_sem:
                desc = job.get("description", "")
                if len(desc) < 400:
                    full_desc = await fetch_full_job_description(client, job["url"])
                    if full_desc:
                        job["description"] = full_desc
                    await asyncio.sleep(0.2)

        await asyncio.gather(*[enrich_job(j) for j in unique_jobs[:35]])

    return unique_jobs
