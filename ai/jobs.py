import json
import logging
import asyncio
from config import GEMINI_API_KEY
from ai.base import extract_json, clean_html

logger = logging.getLogger(__name__)

def get_personal_experience():
    """Читает базу знаний кандидата из файла."""
    try:
        with open("knowledge_base.md", "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.error(f"Error reading knowledge_base.md: {e}")
        return "Sadu Nurzhan. Frontend Developer (Vue/React/TS)."

async def expand_search_query(base_query):
    """Использует Gemini для расширения поискового запроса до 5-7 вариаций."""
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.0-flash') # Используем быструю модель
    
    prompt = f"""Ты — эксперт по подбору персонала. Пользователь ищет вакансии по запросу: "{base_query}".
Твоя задача — сгенерировать 5-7 максимально эффективных поисковых фраз (на английском), которые помогут найти больше релевантных вакансий на международных площадках (LinkedIn, Indeed, Adzuna и т.д.).

Вариации должны включать:
- Названия ролей (Senior Frontend, Vue Engineer, etc.)
- Технологии (Vue 3, TypeScript, Next.js)
- Комбинации с "Remote" и "Worldwide".

Верни ТОЛЬКО JSON список строк:
["фраза 1", "фраза 2", ...]"""

    try:
        response = await model.generate_content_async(prompt)
        if response and response.text:
            variations = extract_json(response.text.strip())
            if variations and isinstance(variations, list):
                # Добавляем оригинальный запрос в начало
                if base_query not in variations:
                    variations.insert(0, base_query)
                return variations[:8]
    except Exception as e:
        logger.error(f"Query expansion error: {e}")
        
    return [base_query]

async def process_job_scoring(job_title, company, description, history=None):
    """Оценивает вакансию с учетом резюме и предыдущих предпочтений пользователя."""
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    # Для вакансий можно использовать 1.5 Pro если нужно больше качества
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    cv_summary = get_personal_experience()
    
    history_context = ""
    if history:
        liked = "\n".join([f"- {h['title']} в {h['company']}" for h in history.get('liked', [])])
        disliked = "\n".join([f"- {h['title']} в {h['company']}" for h in history.get('disliked', [])])
        if liked: history_context += f"\nПользователю РАНЕЕ ПОНРАВИЛИСЬ эти вакансии:\n{liked}"
        if disliked: history_context += f"\nПользователь РАНЕЕ ОТКЛОНИЛ эти вакансии:\n{disliked}"

    prompt = f"""Ты — HR-эксперт по международному найму. Твоя задача — объективно оценить вакансию для FRONTEND РАЗРАБОТЧИКА.
{job_title} в компании {company}.

Описание/Стек:
{description[:3500]}

Профиль кандидата:
{cv_summary}
{history_context}

КРИТЕРИИ ОЦЕНКИ:
1. ПРОВЕРЬ РОЛЬ: 
   - Мы ищем Frontend (Vue, React, Next.js, TypeScript).
   - Если это чисто Backend, DevOps, QA, Data Science — ставь score 0.
   - Fullstack допустим, если Frontend > 50%.
2. ОЦЕНИ СТЕК (0-10):
   - Vue 3 + TypeScript: приоритет №1 (8-10 баллов).
   - React + TypeScript + Next.js: приоритет №2 (7-9 баллов).
   - Если стек современный и совпадает с опытом, ставь высокий балл.
   - Устаревший стек (Vue 2, jQuery, Angular 1): score 0-3.
3. ПРОВЕРЬ УРОВЕНЬ:
   - Мы рассматриваем Middle, Middle+ и Senior. Это ок.
4. ПРОВЕРЬ ЛОКАЦИЮ:
   - Если вакансия требует работы в офисе (кроме Казахстана) — ставь score 0.
   - Remote (Worldwide/CIS/Europe) — это отлично.
5. БОНУС ЗА СВЕЖЕСТЬ:
   - Если вакансия свежая, накидывай +1 балл (до макс 10).

Верни ТОЛЬКО JSON:
{{
  "score": 0,
  "is_worldwide": true/false,
  "core_stack_match": true,
  "matching_skills": [],
  "missing_skills": [],
  "location_reason": "...",
  "verdict": "...",
  "has_salary": false
}}"""

    try:
        response = await model.generate_content_async(prompt)
        if not response or not response.text:
            return {"score": 0, "is_worldwide": False, "location_reason": "Error", "verdict": "Ошибка ИИ", "has_salary": False}
            
        data = extract_json(response.text.strip())
        if data:
            return {
                "score": int(data.get("score", 0)),
                "is_worldwide": bool(data.get("is_worldwide", False)),
                "core_stack_match": bool(data.get("core_stack_match", False)),
                "matching_skills": list(data.get("matching_skills", [])),
                "missing_skills": list(data.get("missing_skills", [])),
                "location_reason": str(data.get("location_reason", "N/A")),
                "verdict": str(data.get("verdict", "Не удалось проанализировать")),
                "has_salary": bool(data.get("has_salary", False))
            }
    except Exception as e:
        logger.error(f"Job scoring error: {e}")
        
    return {
        "score": 0,
        "is_worldwide": False,
        "core_stack_match": False,
        "matching_skills": [],
        "missing_skills": [],
        "location_reason": "Error",
        "verdict": "Ошибка обработки",
        "has_salary": False,
    }

async def suggest_new_companies(prompt_context):
    """Использует Gemini для поиска и добавления новых компаний в мониторинг."""
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    prompt = f"""Ты — эксперт по рынку труда в IT. Твоя задача: найти 5-10 компаний, которые соответствуют запросу пользователя.
Для каждой компании найди ПРЯМУЮ ссылку на страницу с вакансиями (career page) или на их профиль в Greenhouse/Lever/Ashby.

Запрос пользователя: {prompt_context}

Верни ТОЛЬКО JSON список объектов:
[
  {{
    "name": "Название компании",
    "url": "https://company.com/careers",
    "keywords": ["Frontend", "Vue", "React"]
  }}
]"""

    try:
        from database import add_target_company
        response = await model.generate_content_async(prompt)
        if response and response.text:
            companies = extract_json(response.text.strip())
            if companies and isinstance(companies, list):
                added_count = 0
                for c in companies:
                    if await add_target_company(c['name'], c['url'], c.get('keywords', [])):
                        added_count += 1
                return added_count, companies
    except Exception as e:
        logger.error(f"Discovery error: {e}")
    return 0, []

async def generate_cover_letter(job_title, company, description):
    """Генерирует лаконичное сопроводительное письмо на языке вакансии."""
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    cv_summary = get_personal_experience()

    prompt = f"""Ты — HR-эксперт. Напиши ОЧЕНЬ короткое сопроводительное письмо (Cover Letter).
Пиши письмо на том же языке, на котором написана вакансия.

Кандидат: Sadu Nurzhan
Опыт и кейсы:
{cv_summary}

Вакансия: {job_title} in {company}
Описание вакансии:
{description[:3000]}

Твоя задача:
1. Выбери из "Опыта и кейсов" ОДИН наиболее подходящий проект.
2. Текст должен состоять СТРОГО ИЗ ОДНОГО АБЗАЦА (3-5 предложений).
3. Суть: Почему мой конкретный опыт принесет пользу {company}.
4. Никаких формальных "шапок", только само письмо.

Верни ТОЛЬКО текст письма."""

    try:
        response = await model.generate_content_async(prompt)
        if response and response.text:
            return response.text.strip()
    except Exception as e:
        logger.error(f"Cover letter generation error: {e}")
        
    return "Failed to generate cover letter."
