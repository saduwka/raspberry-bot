"""Фильтрация ролей по title вакансии — только frontend (и fullstack с FE-стеком)."""

_FRONTEND_MARKERS = (
    "frontend",
    "front-end",
    "front end",
    "vue",
    "nuxt",
    "react",
    "svelte",
    "фронтенд",
    "фронтэнд",
    "фронт-енд",
    "ui engineer",
    "ui developer",
    "web developer",
    "javascript developer",
    "typescript developer",
)

_FULLSTACK_MARKERS = ("full-stack", "full stack", "fullstack")

_HARD_EXCLUDED = (
    "support engineer",
    "customer support",
    "success engineer",
    "developer success",
    "solutions architect",
    "site engineer",
    "commercial sales",
    "product marketing",
    "security incident",
    "program lead",
    " startups program",
    "representative",
    "account executive",
    "account manager",
    "business development",
    "head of developer community",
    "head of product marketing",
    "head of northern europe",
    "director, commercial",
    "director, customer",
    "manager, security",
)

_SOFT_EXCLUDED = (
    "director",
    "head of",
    "vice president",
    " vp ",
    "chief ",
    "sales",
    "marketing",
    "customer success",
    "community",
    "scrum master",
    "product manager",
    "project manager",
    "recruiter",
    "talent",
    "people ops",
    "designer",
    "content writer",
    "copywriter",
    "legal",
    "finance",
    "accountant",
)

_TECH_EXCLUDED = (
    "backend",
    "back-end",
    "devops",
    "qa engineer",
    "tester",
    "android",
    "ios",
    "swift",
    "kotlin",
    "java developer",
    "python developer",
    "php developer",
    "c++",
    "c#",
    ".net",
    "ruby",
    "rust",
    "golang",
    "embedded",
    "firmware",
    "hardware",
    "data scientist",
    "data engineer",
    "ml engineer",
    "machine learning",
    "ai engineer",
    "ai-native",
    "oracle developer",
    "shopify developer",
)

_NODE_ONLY = ("node.js", "nodejs", "node js", "node developer")


def has_frontend_marker(title: str) -> bool:
    if not title or not title.strip():
        return False
    t = title.lower().replace("node.js", "nodejs").replace("node js", "nodejs")
    return any(m in t for m in _FRONTEND_MARKERS)


def is_fullstack_title(title: str) -> bool:
    t = (title or "").lower()
    return any(m in t for m in _FULLSTACK_MARKERS)


# Бэкенд-языки в title: «Fullstack .NET/Vue», «Java/Vue», «Golang+React» — не наш профиль.
_BACKEND_STACK = (
    ".net", "c#", "java ", "java/", "java,", "(java", "/java", "kotlin", "php", "laravel", "symfony",
    "python", "django", "golang", " go ", "go+", "+go", "(go", "/go)", "go/", "ruby", "rails", "scala",
    "elixir", "rust", "c++", "delphi", "oracle", "1с", "1c ", "bitrix", "битрикс", "salesforce", "sap ",
)
# Конструкторы/CMS и узкие платформы.
_CMS = ("wordpress", "magento", "shopify", "hubspot", "webflow", "drupal", "joomla", "tilda", "squarespace")
# Не разработка, даже с FE в title.
_NON_DEV = (
    "manager", "менеджер", "руководитель", "head of", "director", "директор", "analyst", "аналитик",
    "designer", "дизайнер", "qa ", " qa", "tester", "тестировщик", "recruiter", "рекрутер", "sales",
    "marketing", "teacher", "преподаватель", "наставник", "mentor", "tutor", "ментор", "consultant",
    "intern", "стажер", "стажёр", "trainee", "working student", "werkstudent", "praktikum",
)
# Признаки вакансии на языке, которым кандидат не работает.
_FOREIGN_LANG = (
    "(m/w/d)", "(w/m/d", "(m/f/d)", "(f/m/d)", "(d/m/w)", "m/w/d", "w/m/d", " h/f", "(h/f)", "f/h",
    "entwickler", "utvecklare", "développeur", "developpeur", "concepteur", "desarrollador",
    "sviluppatore", "ontwikkelaar", "programista", "vývojář", "fejlesztő", "kehittäjä", "udvikler",
)


def title_reject_reason(title: str) -> str | None:
    """Причина отказа по title или None, если title подходит."""
    if not title or not title.strip():
        return "пустой title"
    t = f" {title.lower()} "
    if any(m in t for m in _FOREIGN_LANG):
        return "язык вакансии"
    if any(m in t for m in _CMS):
        return "CMS/конструктор"
    if any(m in t for m in _NON_DEV):
        return "не разработка"
    if any(m in t for m in _BACKEND_STACK):
        return "бэкенд-стек в title"
    if "angular" in t and not any(m in t for m in ("vue", "react")):
        return "Angular"
    if not require_frontend_title(title):
        return "не frontend"
    return None


def require_frontend_title(title: str) -> bool:
    """True только для frontend или fullstack+FE маркера в title."""
    if not title or not title.strip():
        return False
    t = f" {(title or '').lower()} "

    if any(m in t for m in _HARD_EXCLUDED):
        return False
    if any(m in t for m in _SOFT_EXCLUDED) and not has_frontend_marker(title):
        return False

    if any(m in t for m in _NODE_ONLY) and not has_frontend_marker(title):
        return False

    if any(m in t for m in _TECH_EXCLUDED) and not has_frontend_marker(title):
        return False

    if has_frontend_marker(title):
        return True

    # fullstack alone is not enough
    return False


def is_developer_role(title: str) -> bool:
    """Совместимость: developer-роль = проходит FE-gate."""
    return require_frontend_title(title)


def is_excluded_role(title: str) -> bool:
    """Title — явно не frontend."""
    return not require_frontend_title(title)


def passes_title_filter(title: str) -> bool:
    """Комбинированный фильтр для fetch/score."""
    return title_reject_reason(title) is None
