"""Источники вакансий v2.

Каждый источник возвращает список словарей _job(...). Источник никогда не бросает исключение
наружу: ошибки фиксируются в SourceStats, чтобы в Telegram было видно, какой портал отвалился.
"""
import asyncio
import html as html_lib
import json
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field

import feedparser
import httpx
from bs4 import BeautifulSoup

from ai.job_location import check_location
from ai.job_role_filters import title_reject_reason
from config import HH_ACCESS_TOKEN, HH_HOST, JOB_ATS_BOARDS, JOB_QUERIES

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"}
_TRACKING = re.compile(r"^(utm_|ref$|source$|from$|hhtmFrom|hhtmFromLabel|query$)", re.I)


def clean_url(url: str) -> str | None:
    if not url:
        return None
    parts = urllib.parse.urlsplit(url.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if not _TRACKING.match(k)]
    path = parts.path.rstrip("/") or "/"
    host = parts.netloc.lower()
    # hh: astana.hh.kz/vacancy/1 и hh.ru/vacancy/1 — одна вакансия
    m = re.match(r"^(?:[\w-]+\.)?hh\.(?:ru|kz)$", host)
    if m and path.startswith("/vacancy/"):
        return f"https://hh.ru{path}"
    return urllib.parse.urlunsplit((parts.scheme or "https", host, path, urllib.parse.urlencode(q), ""))


def strip_html(text: str, limit: int = 3000) -> str:
    if not text:
        return ""
    if "<" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()[:limit]


_CIS_SOURCES = {"hh", "Habr", "getmatch", "DOU", "Djinni"}


def _job(*, title, company, url, source, location="", description="", salary="", remote=None, countries=None):
    title = re.sub(r"\s+", " ", html_lib.unescape(title or "")).strip()
    location = re.sub(r"\s+", " ", html_lib.unescape(location or "")).strip()
    description = strip_html(description)
    ok, loc_label = check_location(location, description, remote=remote, countries=countries,
                                   trusted_remote=source in _CIS_SOURCES)
    return {
        "title": title,
        "company": (company or "").strip() or "—",
        "url": clean_url(url),
        "salary_raw": (salary or "").strip() or "Не указана",
        "is_remote": 1 if (remote or "remote" in loc_label.lower()) else 0,
        "location": loc_label,
        "location_ok": ok,
        "source": source,
        "description": (f"Локация: {loc_label}\n" + description)[:3000],
    }


@dataclass
class SourceStats:
    name: str
    found: int = 0       # всего записей у источника
    relevant: int = 0    # прошли фильтр роли+локации
    error: str = ""
    seconds: float = 0.0
    rejected: dict = field(default_factory=dict)

    def as_dict(self):
        return {"name": self.name, "found": self.found, "relevant": self.relevant,
                "error": self.error, "seconds": round(self.seconds, 1),
                "rejected": dict(sorted(self.rejected.items(), key=lambda x: -x[1])[:4])}


def _keep(stats: SourceStats, jobs: list[dict]) -> list[dict]:
    out, seen = [], set()
    stats.found += len(jobs)
    for j in jobs:
        if not j.get("url") or j["url"] in seen:
            continue
        seen.add(j["url"])
        reason = title_reject_reason(j["title"])
        if reason is None and not j.get("location_ok", True):
            reason = "локация"
        if reason:
            stats.rejected[reason] = stats.rejected.get(reason, 0) + 1
            continue
        out.append(j)
    stats.relevant += len(out)
    return out


def _salary_hh(comp):
    if not comp:
        return ""
    cur = {"RUR": "₽", "KZT": "₸", "USD": "$", "EUR": "€"}.get(comp.get("currencyCode"), comp.get("currencyCode") or "")
    lo, hi = comp.get("from"), comp.get("to")
    rng = f"{lo:,}–{hi:,}" if lo and hi else (f"от {lo:,}" if lo else (f"до {hi:,}" if hi else ""))
    if not rng:
        return ""
    gross = " gross" if comp.get("gross") else ""
    return f"{rng} {cur}{gross}".replace(",", " ")


# ---------------- hh.ru / hh.kz ----------------

async def _hh_api(client, text, area=None, remote=True):
    params = {"text": text, "search_field": "name", "per_page": 50, "order_by": "publication_time",
              "period": 3, "professional_role": 96}
    if remote:
        params["work_format"] = "REMOTE"
    if area:
        params["area"] = area
    r = await client.get("https://api.hh.ru/vacancies", params=params,
                         headers={"Authorization": f"Bearer {HH_ACCESS_TOKEN}",
                                  "HH-User-Agent": "raspberry-bot/2.0 (nurzhan13071996@gmail.com)"})
    r.raise_for_status()
    out = []
    for v in r.json().get("items", []):
        formats = " ".join(x.get("id", "") for x in (v.get("work_format") or []))
        sal = v.get("salary_range") or v.get("salary") or {}
        comp = {"from": sal.get("from"), "to": sal.get("to"), "currencyCode": sal.get("currency"),
                "gross": sal.get("gross")} if sal else None
        area_name = (v.get("area") or {}).get("name", "")
        is_rem = "REMOTE" in formats or (v.get("schedule") or {}).get("id") == "remote"
        out.append(_job(
            title=v.get("name"), company=(v.get("employer") or {}).get("name"), url=v.get("alternate_url"),
            source="hh", remote=is_rem, salary=_salary_hh(comp),
            location=f"{'Remote' if is_rem else 'Офис/гибрид'}, {area_name}",
            description=" ".join(filter(None, [(v.get("snippet") or {}).get("requirement"),
                                               (v.get("snippet") or {}).get("responsibility"),
                                               (v.get("experience") or {}).get("name")])),
        ))
    return out


async def _hh_web(client, text, area=None, remote=True):
    params = {"text": text, "search_field": "name", "order_by": "publication_time", "search_period": 3,
              "items_on_page": 50, "professional_role": 96}
    if remote:
        params["work_format"] = "REMOTE"
    if area:
        params["area"] = area
    r = await client.get(f"https://{HH_HOST}/search/vacancy", params=params)
    r.raise_for_status()
    m = re.search(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', r.text, re.S)
    if not m:
        raise RuntimeError("hh: нет InitialState (капча или смена вёрстки)")
    data = json.loads(html_lib.unescape(m.group(1)))
    out = []
    for v in (data.get("vacancySearchResult") or {}).get("vacancies", []):
        formats = json.dumps(v.get("workFormats") or [], ensure_ascii=False)
        is_rem = "REMOTE" in formats or v.get("@workSchedule") == "remote"
        hybrid = "HYBRID" in formats
        area_name = (v.get("area") or {}).get("name", "")
        fmt = "Remote" if is_rem else ("Гибрид" if hybrid else "Офис")
        exp = v.get("workExperience") or ""
        exp = {"noExperience": "без опыта", "between1And3": "опыт 1–3 года", "between3And6": "опыт 3–6 лет",
               "moreThan6": "опыт 6+ лет"}.get(exp, exp)
        company = (v.get("company") or {}).get("visibleName") or (v.get("company") or {}).get("name")
        url = (v.get("links") or {}).get("desktop") or f"https://hh.ru/vacancy/{v.get('vacancyId')}"
        fmt_text = ("удалённо (город работодателя: " + area_name + ", для удалёнки не важен)") if is_rem \
            else f"{fmt.lower()}, {area_name}"
        out.append(_job(
            title=v.get("name"), company=company, url=url, source="hh", remote=is_rem,
            salary=_salary_hh(v.get("compensation")), location=f"{fmt}, {area_name}",
            description=f"Формат: {fmt_text}. Требуемый {exp}.",
        ))
    return out


_LEGACY_DEFAULT_QUERIES = {"vue 3 nuxt typescript frontend remote", "vue typescript frontend"}
_queries = list(JOB_QUERIES)


async def load_queries():
    """Запросы из /job_query (через запятую) или JOB_QUERIES из .env."""
    from trade.repo import get_trade_state
    global _queries
    raw = await get_trade_state("job_search_query")
    raw = (raw or "").strip().strip('"').strip("'")
    if raw and raw.lower() not in _LEGACY_DEFAULT_QUERIES:
        parts = [x.strip() for x in re.split(r"[,;\n]", raw) if x.strip()]
        _queries = parts[:6] or list(JOB_QUERIES)
    else:
        _queries = list(JOB_QUERIES)
    return _queries


async def fetch_hh(client, stats):
    fn = _hh_api if HH_ACCESS_TOKEN else _hh_web
    jobs = []
    plans = [(q, None, True) for q in _queries] + [("frontend OR vue OR react OR фронтенд", 159, False)]
    for text, area, remote in plans:
        try:
            jobs += await fn(client, text, area=area, remote=remote)
        except Exception as e:
            stats.error = f"{type(e).__name__}: {str(e)[:120]}"
            break
        await asyncio.sleep(2.0)
    return jobs


# ---------------- Хабр Карьера ----------------

async def fetch_habr(client, stats):
    jobs = []
    for q in _queries:
        r = await client.get("https://career.habr.com/vacancies",
                             params={"q": q, "type": "all", "remote": "true", "sort": "date"})
        r.raise_for_status()
        m = re.search(r'<script[^>]*data-ssr-state[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            raise RuntimeError("habr: нет ssr-state")
        state = json.loads(html_lib.unescape(m.group(1)))
        items = ((state.get("vacancies") or {}).get("list")) or []
        for v in items:
            company = (v.get("company") or {}).get("title")
            locs = ", ".join(x.get("title", "") for x in (v.get("locations") or []))
            skills = ", ".join(x.get("title", "") for x in (v.get("skills") or []))
            sal = (v.get("salary") or {}).get("formatted") or ""
            remote = bool(v.get("remoteWork"))
            loc = ("Remote" + (f" / {locs}" if locs else "")) if remote else locs
            jobs.append(_job(
                title=v.get("title"), company=company, url="https://career.habr.com" + (v.get("href") or ""),
                source="Habr", remote=remote, salary=sal, location=loc,
                description=f"{(v.get('salaryQualification') or {}).get('title', '')}. Навыки: {skills}. "
                            f"{(v.get('employment') or '')}",
            ))
        await asyncio.sleep(1.5)
    return jobs


# ---------------- getmatch ----------------

async def fetch_getmatch(client, stats):
    r = await client.get("https://getmatch.ru/api/offers", params={"pa": "all", "limit": 100})
    r.raise_for_status()
    jobs = []
    for o in r.json().get("offers", []):
        loc_req = o.get("location_requirements") or []
        remote = (o.get("remote_options") in ("full", "anywhere")) or bool(o.get("is_remote"))
        locs = ", ".join(x.get("location", x) if isinstance(x, dict) else str(x) for x in (o.get("display_locations") or []))
        sal = ""
        if o.get("salary_display_from") or o.get("salary_display_to"):
            sal = f"{o.get('salary_display_from') or ''}–{o.get('salary_display_to') or ''} {o.get('salary_currency') or ''}".strip("– ")
        desc = strip_html(o.get("offer_description") or o.get("short_description") or "", 2000)
        req = json.dumps(loc_req, ensure_ascii=False)[:300] if loc_req else ""
        jobs.append(_job(
            title=o.get("position"), company=(o.get("company") or {}).get("name"),
            url="https://getmatch.ru" + o["url"] if str(o.get("url", "")).startswith("/") else o.get("url"),
            source="getmatch", remote=remote or None, salary=sal,
            location=("Remote" if remote else "") + (f" {locs}" if locs else ""),
            description=f"{desc} {req}",
        ))
    return jobs


# ---------------- RSS: DOU, Djinni, WWR ----------------

async def _rss(client, url, source, company_from_title=False):
    r = await client.get(url)
    r.raise_for_status()
    feed = feedparser.parse(r.text)
    out = []
    for e in feed.entries[:80]:
        title = e.get("title", "")
        company = e.get("author", "")
        location = ""
        salary_dou = ""
        if company_from_title and ":" in title:
            company, title = [x.strip() for x in title.split(":", 1)]
        if source == "DOU":
            # «Frontend Developer в Company, удаленно, $3000–4000»
            m = re.match(r"^(.*?)\s+в\s+(.+?)(?:,\s*(.*))?$", title)
            if m:
                title, company, location = m.group(1), m.group(2), html_lib.unescape(m.group(3) or "")
                sal = re.search(r"((?:до|від|от)?\s*\$[\d\s–-]+)", location)
                if sal:
                    salary_dou = sal.group(1).strip()
                    location = location.replace(sal.group(1), "").strip(" ,")
                if "віддалено" in location or "удаленно" in location:
                    location = "Remote" + (f" / {location}" if location not in ("віддалено", "удаленно") else "")
        desc = e.get("summary") or e.get("description") or ""
        if source == "WWR":
            location = e.get("region", "") or ""
        out.append(_job(title=title, company=company, url=e.get("link"), source=source,
                        location=location, description=desc,
                        salary=salary_dou,
                        remote=True if source in ("WWR", "Remotive") else None))
    return out


async def fetch_dou(client, stats):
    return await _rss(client, "https://jobs.dou.ua/vacancies/feeds/?category=Front+End&remote", "DOU")


async def fetch_djinni(client, stats):
    jobs = []
    for kw in ("JavaScript", "React.js", "Vue.js"):
        try:
            jobs += await _rss(client, f"https://djinni.co/jobs/rss/?primary_keyword={kw}&employment=remote", "Djinni")
        except Exception as e:
            stats.error = str(e)[:100]
    return jobs


async def fetch_wwr(client, stats):
    return await _rss(client, "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
                      "WWR", company_from_title=True)


# ---------------- Remotive / Himalayas ----------------

async def fetch_remotive(client, stats):
    r = await client.get("https://remotive.com/api/remote-jobs", params={"category": "software-dev", "limit": 150})
    r.raise_for_status()
    return [_job(title=j.get("title"), company=j.get("company_name"), url=j.get("url"), source="Remotive",
                 remote=True, salary=j.get("salary") or "", location=j.get("candidate_required_location") or "",
                 description=j.get("description") or "")
            for j in r.json().get("jobs", [])]


async def fetch_himalayas(client, stats):
    jobs = []
    for q in ("frontend", "vue", "react"):
        r = await client.get("https://himalayas.app/jobs/api/search", params={"q": q, "limit": 50, "sort": "recent"})
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            countries = j.get("locationRestrictions") or []
            countries = [c.get("name", c) if isinstance(c, dict) else c for c in countries]
            sal = ""
            if j.get("minSalary") or j.get("maxSalary"):
                sal = f"{j.get('minSalary') or ''}–{j.get('maxSalary') or ''} {j.get('currency') or ''}".strip("– ")
            jobs.append(_job(title=j.get("title"), company=j.get("companyName"),
                             url=j.get("applicationLink") or j.get("guid"), source="Himalayas", remote=True,
                             salary=sal, countries=countries or None,
                             location="Worldwide" if not countries else "",
                             description=j.get("excerpt") or j.get("description") or ""))
        await asyncio.sleep(1)
    return jobs


# ---------------- ATS: Ashby / Greenhouse / Lever ----------------

async def fetch_ats(client, stats):
    jobs, errors = [], 0
    for board in JOB_ATS_BOARDS:
        kind, _, slug = board.partition(":")
        try:
            if kind == "ashby":
                r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                for j in r.json().get("jobs", []):
                    loc = j.get("location") or ""
                    sec = ", ".join(x.get("location", "") for x in (j.get("secondaryLocations") or []))
                    jobs.append(_job(title=j.get("title"), company=slug, url=j.get("jobUrl"), source="Ashby",
                                     remote=bool(j.get("isRemote")) or None, location=f"{loc} {sec}".strip(),
                                     description=j.get("descriptionPlain") or ""))
            elif kind == "gh":
                r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                for j in r.json().get("jobs", []):
                    jobs.append(_job(title=j.get("title"), company=j.get("company_name") or slug,
                                     url=j.get("absolute_url"), source="Greenhouse",
                                     location=(j.get("location") or {}).get("name", ""),
                                     description=html_lib.unescape(j.get("content") or "")))
            elif kind == "lever":
                r = await client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                for j in r.json():
                    cat = j.get("categories") or {}
                    jobs.append(_job(title=j.get("text"), company=slug, url=j.get("hostedUrl"), source="Lever",
                                     remote=(j.get("workplaceType") == "remote") or None,
                                     location=f"{cat.get('location', '')} {j.get('workplaceType', '')}",
                                     description=j.get("descriptionPlain") or ""))
        except Exception as e:
            errors += 1
            logger.warning("ATS %s failed: %s", board, e)
        await asyncio.sleep(0.3)
    if errors:
        stats.error = f"ошибок досок: {errors}"
    return jobs


async def enrich_hh(client, jobs, limit=30):
    """Подтягивает описание и навыки со страницы вакансии hh (только для новых, прошедших фильтры)."""
    from jobs.repo import is_vacancy_seen
    targets = []
    for j in jobs:
        if j["source"] == "hh" and len(targets) < limit and not await is_vacancy_seen(j["url"]):
            targets.append(j)
    sem = asyncio.Semaphore(2)

    async def one(j):
        async with sem:
            try:
                r = await client.get(j["url"].replace("https://hh.ru", f"https://{HH_HOST}"))
                if r.status_code != 200:
                    return
                m = re.search(r'data-qa="vacancy-description"[^>]*>(.*?)</div>\s*</div>', r.text, re.S)
                skills = re.findall(r'data-qa="skills-element".*?>([^<]{2,40})<', r.text)
                body = strip_html(m.group(1), 2200) if m else ""
                head = j["description"].split("\n", 1)
                j["description"] = (head[0] + "\n" + (head[1] if len(head) > 1 else "") +
                                    (f" Навыки: {', '.join(skills[:15])}." if skills else "") +
                                    (f"\n{body}" if body else ""))[:3000]
            except Exception as e:
                logger.debug("hh enrich %s: %s", j["url"], e)
            await asyncio.sleep(0.7)

    await asyncio.gather(*(one(j) for j in targets))
    return len(targets)


SOURCES = [
    ("hh", fetch_hh),
    ("Habr", fetch_habr),
    ("getmatch", fetch_getmatch),
    ("DOU", fetch_dou),
    ("Djinni", fetch_djinni),
    ("Remotive", fetch_remotive),
    ("Himalayas", fetch_himalayas),
    ("WWR", fetch_wwr),
    ("ATS-доски", fetch_ats),
]


async def fetch_all_sources(progress_callback=None):
    """Возвращает (jobs, stats_list)."""
    results, stats_all = [], []
    await load_queries()
    async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as client:
        async def run(name, fn):
            st = SourceStats(name)
            t0 = time.monotonic()
            try:
                raw = await asyncio.wait_for(fn(client, st), timeout=150)
                kept = _keep(st, raw)
            except Exception as e:
                st.error = f"{type(e).__name__}: {str(e)[:120]}"
                kept = []
                logger.warning("Source %s failed: %s", name, st.error)
            st.seconds = time.monotonic() - t0
            logger.info("Source %s: found=%s relevant=%s err=%s %.1fs", name, st.found, st.relevant,
                        st.error or "-", st.seconds)
            if progress_callback:
                try:
                    await progress_callback(name, st.relevant)
                except Exception:
                    pass
            return kept, st

        done = await asyncio.gather(*(run(n, f) for n, f in SOURCES))
        hh_jobs = [j for kept, st in done if st.name == "hh" for j in kept]
        if hh_jobs:
            try:
                n = await asyncio.wait_for(enrich_hh(client, hh_jobs), timeout=90)
                logger.info("hh enrich: %s vacancies", n)
            except Exception as e:
                logger.warning("hh enrich failed: %s", e)
    seen = set()
    for kept, st in done:
        stats_all.append(st.as_dict())
        for j in kept:
            if j["url"] not in seen:
                seen.add(j["url"])
                results.append(j)
    return results, stats_all
