"""Фильтрация ролей по title вакансии."""

_DEVELOPER_MARKERS = (
    "frontend", "front-end", "front end",
    "vue", "nuxt", "react", "typescript",
    "javascript developer", "js developer",
    "web developer", "ui engineer",
    "software engineer", "software developer",
    "full-stack", "full stack", "fullstack",
    "web engineer",
    "developer", "engineer",
)

_HARD_EXCLUDED = (
    "support engineer", "customer support", "success engineer",
    "developer success", "solutions architect", "site engineer",
    "commercial sales", "product marketing", "security incident",
    "program lead", " startups program", "representative",
    "account executive", "account manager", "business development",
    "head of developer community", "head of product marketing",
    "head of northern europe", "director, commercial",
    "director, customer", "manager, security",
)

_SOFT_EXCLUDED = (
    "director", "head of", "vice president", " vp ", "chief ",
    "sales", "marketing", "customer success", "community",
    "scrum master", "product manager", "project manager",
    "recruiter", "talent", "people ops", "designer",
    "content writer", "copywriter", "legal", "finance", "accountant",
)

_TECH_EXCLUDED = (
    "backend", "back-end", "devops", "qa engineer", "tester",
    "android", "ios", "swift", "kotlin", "java developer", "python developer",
    "php developer", "c++", "c#", ".net", "ruby", "rust", "golang",
    "embedded", "firmware", "hardware", "data scientist", "data engineer",
    "ml engineer", "machine learning",
)

_FULLSTACK_SAVE = ("full-stack", "full stack", "fullstack")

_DEV_SAVE = (
    "frontend", "front-end", "full-stack", "full stack", "fullstack",
    "vue", "nuxt", "react developer", "typescript developer",
    "javascript developer", "software engineer", "software developer",
    "web developer", "ui engineer",
)


def is_developer_role(title: str) -> bool:
    """Title похож на developer/engineer/frontend роль."""
    if not title or not title.strip():
        return False
    t = title.lower()
    return any(m in t for m in _DEVELOPER_MARKERS)


def is_excluded_role(title: str) -> bool:
    """Title — явно не-developer роль."""
    if not title or not title.strip():
        return False
    t = f" {title.lower()} "

    if any(m in t for m in _HARD_EXCLUDED):
        return True

    if any(m in t for m in _TECH_EXCLUDED):
        if not any(m in t for m in _FULLSTACK_SAVE + ("frontend", "front-end")):
            return True

    if any(m in t for m in _DEV_SAVE):
        return False

    if "manager" in t:
        if any(m in t for m in ("frontend", "front-end", "vue", "react", "full-stack", "full stack", "fullstack")):
            return False
        return True

    if any(m in t for m in _SOFT_EXCLUDED):
        return True

    return False


def passes_title_filter(title: str) -> bool:
    """Комбинированный фильтр для fetch: developer role и не excluded."""
    if is_excluded_role(title):
        return False
    return is_developer_role(title)
