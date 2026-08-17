import asyncio
import logging

from ai.client import chat as _local_chat
from ai.job_scoring_rules import cover_letter_template, expand_query_rules, score_job_rules
from core.jsonutil import extract_json
from jobs.repo import add_target_company, get_cached_job_score, save_job_score_cache

logger = logging.getLogger(__name__)


def get_personal_experience():
    try:
        with open("knowledge_base.md", "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.error("Error reading knowledge_base.md: %s", e)
        return "Sadu Nurzhan. Frontend Developer (Vue/React/TS)."


def _normalize_scoring_result(data: dict, mode: str) -> dict:
    return {
        "score": int(data.get("score", 0)),
        "is_worldwide": bool(data.get("is_worldwide", False)),
        "core_stack_match": bool(data.get("core_stack_match", False)),
        "matching_skills": list(data.get("matching_skills", [])),
        "missing_skills": list(data.get("missing_skills", [])),
        "location_reason": str(data.get("location_reason", "N/A")),
        "verdict": str(data.get("verdict", "Не удалось проанализировать")),
        "has_salary": bool(data.get("has_salary", False)),
        "scoring_mode": mode,
    }


def _scoring_prompt(job_title, company, description, history=None):
    cv_summary = get_personal_experience()
    history_context = ""
    if history:
        liked = "\n".join([f"- {h['title']} в {h['company']}" for h in history.get("liked", [])])
        disliked = "\n".join([f"- {h['title']} в {h['company']}" for h in history.get("disliked", [])])
        if liked:
            history_context += f"\nПользователю РАНЕЕ ПОНРАВИЛИСЬ эти вакансии:\n{liked}"
        if disliked:
            history_context += f"\nПользователь РАНЕЕ ОТКЛОНИЛ эти вакансии:\n{disliked}"

    return f"""Ты — HR-эксперт по международному найму. Оцени вакансию для FRONTEND РАЗРАБОТЧИКА.
{job_title} в компании {company}.

Описание/Стек:
{description[:3500]}

Профиль кандидата:
{cv_summary}
{history_context}

КРИТЕРИИ:
1. Title ДОЛЖЕН быть developer/engineer/frontend ролью. Sales/Director/Support/Marketing/Community/PM — score 0, core_stack_match=false (даже если React упомянут в описании компании).
2. Vue 3 / Nuxt 3 + TypeScript remote: 9-10. React + TS remote: 7-8. Generic frontend JS: 5-6.
3. Backend/DevOps/QA/Mobile/Data — score 0.
4. Middle/Senior — ок, 4+ года опыта.
5. Remote Worldwide/CIS/Europe — отлично. On-site only вне Казахстана — score 0.

Верни ТОЛЬКО JSON:
{{
  "score": 0,
  "is_worldwide": true,
  "core_stack_match": true,
  "matching_skills": [],
  "missing_skills": [],
  "location_reason": "...",
  "verdict": "...",
  "has_salary": false
}}"""


def _batch_scoring_prompt(jobs, history=None):
    cv_summary = get_personal_experience()
    history_context = ""
    if history:
        liked = "\n".join([f"- {h['title']} в {h['company']}" for h in history.get("liked", [])])
        disliked = "\n".join([f"- {h['title']} в {h['company']}" for h in history.get("disliked", [])])
        if liked:
            history_context += f"\nПонравились:\n{liked}"
        if disliked:
            history_context += f"\nОтклонены:\n{disliked}"

    blocks = []
    for i, job in enumerate(jobs):
        blocks.append(
            f"--- JOB {i} ---\n"
            f"Title: {job['title']}\n"
            f"Company: {job['company']}\n"
            f"Description:\n{job.get('description', '')[:1200]}"
        )

    return f"""Оцени каждую FRONTEND вакансию для кандидата (Vue 3/Nuxt приоритет, React/TS вторично, remote).
{cv_summary}
{history_context}

ПРАВИЛА: если title не developer/engineer/frontend (sales, director, support, marketing) — score 0.
Vue 3/Nuxt+TS remote: 9-10. React+TS remote: 7-8. On-site only вне KZ: 0.

{chr(10).join(blocks)}

Верни ТОЛЬКО JSON-массив (по одному объекту на JOB index):
[
  {{
    "index": 0,
    "score": 0,
    "is_worldwide": true,
    "core_stack_match": true,
    "matching_skills": [],
    "missing_skills": [],
    "location_reason": "...",
    "verdict": "...",
    "has_salary": false
  }}
]"""


async def _score_batch_with_local(jobs, history=None):
    text = await _local_chat(_batch_scoring_prompt(jobs, history))
    if not text:
        logger.error("AI local failed: batch scoring empty")
        return None
    data = extract_json(text)
    if not isinstance(data, list):
        logger.error("AI local failed: batch scoring JSON is not a list")
        return None
    results = [None] * len(jobs)
    for item in data:
        if not isinstance(item, dict):
            continue
        idx = int(item.get("index", -1))
        if 0 <= idx < len(jobs):
            results[idx] = _normalize_scoring_result(item, "local")
    return results


async def process_jobs_scoring_batch(jobs, history=None, use_cache=True):
    """Батч-скоринг: кеш → local LLM пачками → эвристика."""
    if not jobs:
        return []

    final = [None] * len(jobs)
    jobs_to_score = []
    index_map = []

    if use_cache:
        for i, job in enumerate(jobs):
            cached = await get_cached_job_score(job.get("url", ""))
            if cached:
                cached["scoring_mode"] = "cache"
                final[i] = cached
            else:
                jobs_to_score.append(job)
                index_map.append(i)
    else:
        jobs_to_score = list(jobs)
        index_map = list(range(len(jobs)))

    if not jobs_to_score:
        return final

    scored_slice = [None] * len(jobs_to_score)
    try:
        batch_results = await _score_batch_with_local(jobs_to_score, history)
        if batch_results and any(r and r.get("score", 0) > 0 for r in batch_results):
            for i, job in enumerate(jobs_to_score):
                r = batch_results[i] if batch_results[i] else None
                if not r or r.get("score", 0) <= 0:
                    r = score_job_rules(job["title"], job["company"], job.get("description", ""))
                scored_slice[i] = r
    except Exception as e:
        logger.error("Batch scoring error: %s", e)

    for i, job in enumerate(jobs_to_score):
        result = scored_slice[i] or score_job_rules(
            job["title"], job["company"], job.get("description", "")
        )
        await save_job_score_cache(job.get("url", ""), result)
        final[index_map[i]] = result

    return final


async def expand_search_query(base_query):
    base_query = base_query.strip().strip('"').strip("'")
    prompt = f"""Сгенерируй 5-7 поисковых фраз (на английском) для вакансий по запросу: "{base_query}".
Включи роли (Senior Frontend, Vue Engineer, Nuxt Developer), технологии (Vue 3, Nuxt 3, Composition API, Pinia, TypeScript), Remote/Worldwide.
Верни ТОЛЬКО JSON список строк: ["фраза 1", "фраза 2", ...]"""

    try:
        text = await _local_chat(prompt)
        if text:
            variations = extract_json(text)
            if variations and isinstance(variations, list):
                if base_query not in variations:
                    variations.insert(0, base_query)
                logger.info("Query expansion via local: %s variations", len(variations))
                return variations[:8]
    except Exception as e:
        logger.error("Query expansion error (local): %s", e)

    logger.info("Query expansion fallback: rules")
    return expand_query_rules(base_query)


async def expand_search_query_safe(base_query, timeout: float = 50):
    try:
        return await asyncio.wait_for(expand_search_query(base_query), timeout=timeout)
    except asyncio.TimeoutError:
        logger.error("Query expansion timed out, using rules fallback")
        return expand_query_rules(base_query)


async def process_job_scoring(job_title, company, description, history=None, url=None):
    if url:
        cached = await get_cached_job_score(url)
        if cached:
            cached["scoring_mode"] = "cache"
            return cached

    results = await process_jobs_scoring_batch(
        [{"title": job_title, "company": company, "description": description, "url": url or ""}],
        history=history,
        use_cache=False,
    )
    return results[0] if results else score_job_rules(job_title, company, description)


async def suggest_new_companies(prompt_context):
    prompt = f"""Найди 5-10 IT-компаний по запросу: {prompt_context}
Для каждой — прямая ссылка на career page или Greenhouse/Lever/Ashby.
Верни ТОЛЬКО JSON:
[{{"name": "...", "url": "https://...", "keywords": ["Frontend", "Vue"]}}]"""

    text = await _local_chat(prompt)
    if not text:
        logger.error("AI local failed: company discovery")

    if text:
        companies = extract_json(text)
        if companies and isinstance(companies, list):
            added_count = 0
            for c in companies:
                if await add_target_company(c["name"], c["url"], c.get("keywords", [])):
                    added_count += 1
            return added_count, companies
    return 0, []


async def generate_cover_letter(job_title, company, description):
    cv_summary = get_personal_experience()
    prompt = f"""Напиши короткое cover letter (3-5 предложений, один абзац) на языке вакансии.

Кандидат: Sadu Nurzhan
Опыт: {cv_summary[:2000]}
Вакансия: {job_title} в {company}
Описание: {description[:3000]}

Без шапки, только текст письма."""

    text = await _local_chat(prompt)
    if not text:
        logger.error("AI local failed: cover letter")

    if text:
        return text.strip()
    return cover_letter_template(job_title, company)
