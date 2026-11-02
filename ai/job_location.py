"""Проверка локации/формата вакансии для кандидата из Астаны (UTC+5, гражданство KZ)."""
import re

HOME_CITIES = ("астана", "astana", "нур-султан", "nur-sultan")

_ALLOW = (
    "worldwide", "anywhere", "global", "globally", "any location", "any country", "work from anywhere",
    "emea", "cis", "снг", "kazakhstan", "казахстан", "central asia", "asia", "utc+5", "gmt+5",
    "без гео", "из любой", "любой стран", "любая стран", "не важно", "весь мир", "all countries",
)
# Явные ограничения по стране/региону, которые кандидату не подходят.
_DENY = (
    "us only", "usa only", "u.s. only", "us-only", "us based", "us-based", "u.s. based", "u.s.-based",
    "united states only", "must reside in the us", "must be located in the us", "us citizens",
    "us residents", "within the us", "in the united states", "north america", "americas", "latam",
    "latin america", "canada only", "uk only", "uk-based", "uk based", "uk residents", "united kingdom only",
    "eu only", "eu-based", "eu residents", "eu work permit", "right to work in the uk", "right to work in the eu",
    "india only", "philippines", "brazil only", "mexico only", "australia only", "nz only", " pst ", "(pst", "est hours",
    "us time zones", "us hours", "us east", "us west", "только рф", "только россия", "гражданство рф",
    "налоговый резидент рф", "резидент рф", "находиться в рф", "находиться на территории рф",
    "только для граждан рф", "только граждане рф",
)
_ONSITE = ("on-site", "onsite", "on site", "in-office", "in office", "office-based", "офис", "hybrid", "гибрид")
_REMOTE = ("remote", "home based", "home-based", "удален", "удалён", "удаленно", "удалённо", "distributed", "work from home", "wfh",
           "полностью удал", "full remote", "fully remote")


# Страны/регионы в строке локации, которые означают «remote только внутри страны».
_COUNTRY_ONLY = (
    "poland", "portugal", "united kingdom", "uk)", " uk", "england", "london", "estonia", "latvia", "lithuania",
    "germany", "berlin", "spain", "france", "netherlands", "belgium", "ireland", "italy", "sweden", "norway",
    "denmark", "finland", "czech", "romania", "bulgaria", "greece", "croatia", "serbia", "hungary", "austria",
    "switzerland", "india", "pakistan", "bangladesh", "philippines", "vietnam", "indonesia", "singapore",
    "japan", "korea", "australia", "new zealand", "usa", "united states", " us)", "(us", " us ", "canada",
    "mexico", "brazil", "argentina", "colombia", "chile", "peru", "latam", "south africa", "nigeria", "kenya",
    "egypt", "israel", "turkey", "uae", "dubai", "europe", "european", "eu)", "(eu", " eu ", "americas",
    "north america", "apac",
)


def _has(text, markers):
    return any(m in text for m in markers)


def check_location(location: str = "", description: str = "", *, remote=None, countries=None,
                   trusted_remote=False):
    """Возвращает (ok: bool, label: str).

    location — строка локации из источника, description — текст вакансии,
    remote — явный флаг из API (True/False/None), countries — список стран из API (Himalayas и т.п.),
    trusted_remote — источник СНГ (hh/Хабр/DOU), где «Remote, Москва» обычно значит «удалёнка, офис в Москве».
    """
    loc = (location or "").lower()
    desc = (description or "").lower()[:6000]
    blob = f"{loc}\n{desc}"

    if countries:
        names = {c.lower() for c in countries}
        if names & {"kazakhstan", "казахстан"}:
            return True, "Remote (включая Казахстан)"
        return False, f"Remote только: {', '.join(list(countries)[:4])}{'…' if len(countries) > 4 else ''}"

    in_home = _has(loc, HOME_CITIES)
    is_remote = remote if remote is not None else (_has(loc, _REMOTE) or _has(desc[:1500], _REMOTE))

    if in_home:
        return True, (location or "Астана").strip()[:60]

    if not is_remote:
        if loc and _has(loc, _ONSITE + ("москва", "moscow", "санкт", "алматы", "almaty", "минск", "киев", "kyiv")):
            return False, f"Офис/гибрид: {location[:50]}"
        if loc and not _has(loc, _ALLOW):
            return False, f"Не remote: {location[:50]}"

    if _has(blob, _DENY) and not _has(blob, ("kazakhstan", "казахстан", "worldwide", "anywhere", "cis", "снг")):
        m = next(x for x in _DENY if x in blob)
        return False, f"Ограничение: {m}"

    if _has(loc, _ALLOW):
        return True, (location or "Remote").strip()[:60]
    if _has(f" {loc} ", _COUNTRY_ONLY) and not trusted_remote:
        return False, f"Remote только: {location[:50]}"
    if is_remote:
        label = (location or "").strip()
        return True, (f"Remote ({label[:45]})" if label and "remote" not in label.lower() else (label[:60] or "Remote"))
    return True, (location or "Локация не указана").strip()[:60]
