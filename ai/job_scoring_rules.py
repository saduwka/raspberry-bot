"""Эвристический скоринг вакансий без внешнего ИИ."""

import re

from ai.job_role_filters import is_developer_role, is_excluded_role

_FRONTEND_ROLES = (
    "frontend", "front-end", "front end", "ui engineer", "web developer",
    "javascript developer", "typescript developer", "vue", "react", "nuxt",
)
_VUE_MARKERS = ("vue 3", "vue3", "composition api", "pinia", "nuxt 3", "nuxt3", "nuxt")
_REACT_MARKERS = ("react", "next.js", "nextjs", "redux")
_TS_MARKERS = ("typescript", " typecript", " ts ", " ts,", " ts.")
_REMOTE_MARKERS = (
    "remote", "worldwide", "distributed", "work from anywhere",
    "remote-first", "home-based", "удален", "удалён", "удаленно",
)
_OFFICE_BLOCKERS = (
    "on-site only", "onsite only", "office only", "in-office only",
    "must be located in", "relocation required",
)
_SALARY_MARKERS = ("$", "€", "₽", "salary", "compensation", "зарплат", "до ", "от ")


def _blob(title: str, description: str) -> str:
    return f"{title}\n{description}".lower()


def score_job_rules(job_title: str, company: str, description: str) -> dict:
    """Возвращает тот же формат, что и process_job_scoring."""
    title_lower = job_title.lower()

    if is_excluded_role(job_title):
        return _result(
            0, False, False, [], ["не frontend-роль"],
            "Локация не проверялась", "Роль не developer — отклонено эвристикой",
            _has_salary(_blob(job_title, description)),
        )

    if not is_developer_role(job_title):
        return _result(
            0, False, False, [], ["title не developer"],
            "Локация не проверялась", "Title не похож на developer/engineer роль",
            _has_salary(_blob(job_title, description)),
        )

    text = _blob(job_title, description)
    matching = []
    missing = []
    score = 5
    core_stack_match = False
    is_worldwide = any(m in text for m in _REMOTE_MARKERS)

    is_frontend = any(r in title_lower for r in _FRONTEND_ROLES) or any(
        r in text for r in _FRONTEND_ROLES
    )
    if not is_frontend:
        return _result(
            0, is_worldwide, False, [], ["frontend не обнаружен"],
            "Remote" if is_worldwide else "Локация неясна",
            "Не похоже на frontend-вакансию", _has_salary(text),
        )

    vue_hit = any(m in text for m in _VUE_MARKERS) or "vue" in title_lower
    nuxt_hit = "nuxt" in text or "nuxt" in title_lower
    react_hit = any(m in text for m in _REACT_MARKERS)
    ts_hit = any(m in text for m in _TS_MARKERS) or "typescript" in title_lower

    if vue_hit or nuxt_hit:
        matching.extend(["Vue", "Nuxt" if nuxt_hit else "", "TypeScript" if ts_hit else ""])
        score = 10 if (vue_hit or nuxt_hit) and ts_hit else 9
        core_stack_match = True
    elif react_hit and ts_hit:
        matching.extend(["React", "TypeScript"])
        score = 8
        core_stack_match = True
    elif react_hit or ts_hit:
        matching.append("React" if react_hit else "TypeScript")
        score = 7
        core_stack_match = True
    else:
        matching.append("Frontend")
        score = 6
        core_stack_match = True
        missing.append("стек не уточнён")

    if is_worldwide:
        score = min(10, score + 1)
    elif any(b in text for b in _OFFICE_BLOCKERS):
        score = 0
        core_stack_match = False

    matching = [m for m in matching if m]
    verdict = (
        f"Эвристика: {'/'.join(matching) or 'frontend'}, "
        f"оценка {score}/10"
    )
    loc = "Remote/Worldwide" if is_worldwide else "Локация: проверьте вручную"

    return _result(score, is_worldwide, core_stack_match, matching, missing, loc, verdict, _has_salary(text))


def expand_query_rules(base_query: str) -> list[str]:
    """Простые вариации запроса без ИИ."""
    q = base_query.strip().strip('"').strip("'")
    variations = [
        q,
        f"Senior Vue 3 TypeScript Remote",
        f"Vue 3 Nuxt Remote",
        f"Senior Frontend {q}",
        f"Remote {q}",
        f"{q} Engineer",
        "Frontend Vue Composition API Remote",
        "React TypeScript Senior Remote",
        "Frontend Developer Remote Worldwide",
    ]
    seen = set()
    out = []
    for v in variations:
        key = v.lower()
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out[:8]


def cover_letter_template(job_title: str, company: str) -> str:
    return (
        f"I am a Middle/Senior Frontend Developer with 4+ years of experience in Vue 3, "
        f"Nuxt, TypeScript, and React. I build performant interfaces, micro-frontends "
        f"(Module Federation), and work comfortably with remote teams worldwide from "
        f"Astana (UTC+5). I would be glad to contribute to {company} as a {job_title}."
    )


def _has_salary(text: str) -> bool:
    return any(m in text for m in _SALARY_MARKERS)


def _result(score, is_worldwide, core_stack_match, matching, missing, loc, verdict, has_salary):
    return {
        "score": int(score),
        "is_worldwide": bool(is_worldwide),
        "core_stack_match": bool(core_stack_match),
        "matching_skills": matching,
        "missing_skills": missing,
        "location_reason": loc,
        "verdict": verdict,
        "has_salary": bool(has_salary),
        "scoring_mode": "rules",
    }
