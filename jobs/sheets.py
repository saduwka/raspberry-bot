"""Google Sheets sync for job vacancies → worksheet Jobs (existing tracker schema)."""
from __future__ import annotations

import logging
import time
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Existing Frontend Job Tracker columns
HEADERS = [
    "Дата найдена",
    "Компания",
    "Должность",
    "Стек",
    "ЗП",
    "Локация / формат",
    "Оценка/10",
    "URL вакансии",
    "Пакет",
    "Статус",
    "Дата отклика",
    "Вернуться к (дата)",
    "Комментарий",
]

STATUS_TO_SHEET = {
    "new": "Новая",
    "dismissed": "В архиве",
    "applied": "Откликнулся",
}
SHEET_TO_STATUS = {v: k for k, v in STATUS_TO_SHEET.items()}

_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
)

_lock = threading.RLock()
_worksheet = None
_url_row_cache: dict[str, int] | None = None

URL_COL = 8  # 1-based column H
STATUS_COL = 10  # J
APPLIED_COL = 11  # K


def _cfg() -> tuple[str, Path, str]:
    from config import (
        GOOGLE_SERVICE_ACCOUNT_FILE,
        GOOGLE_SHEETS_ID,
        GOOGLE_SHEETS_WORKSHEET,
    )

    return (
        (GOOGLE_SHEETS_ID or "").strip(),
        Path(GOOGLE_SERVICE_ACCOUNT_FILE or "/root/bot/secrets/google-sa.json"),
        (GOOGLE_SHEETS_WORKSHEET or "Jobs").strip() or "Jobs",
    )


def is_configured() -> bool:
    sheet_id, sa_path, _ = _cfg()
    return bool(sheet_id) and sa_path.is_file()


def _client_worksheet():
    global _worksheet
    if _worksheet is not None:
        return _worksheet

    sheet_id, sa_path, worksheet_name = _cfg()
    if not sheet_id:
        raise RuntimeError("GOOGLE_SHEETS_ID is empty")
    if not sa_path.is_file():
        raise RuntimeError(f"service account file missing: {sa_path}")

    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(str(sa_path), scopes=_SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=2000, cols=len(HEADERS))
        ws.update("A1", [HEADERS], value_input_option="RAW")
    _worksheet = ws
    return ws


_headers_checked = False
_next_row: int | None = None
_cache_at = 0.0
_CACHE_TTL = 600  # строки могли добавить вручную — перечитываем колонку URL раз в 10 минут


def _ensure_headers(ws) -> None:
    global _headers_checked
    if _headers_checked:
        return
    values = ws.row_values(1)
    _headers_checked = True
    if not values:
        ws.update("A1", [HEADERS], value_input_option="RAW")
        return
    # Never overwrite an existing tracker header row.


def _rebuild_url_cache(ws) -> dict[str, int]:
    global _url_row_cache, _next_row, _cache_at
    urls = ws.col_values(URL_COL)
    _cache_at = time.monotonic()
    _next_row = len(urls) + 1
    cache: dict[str, int] = {}
    for idx, url in enumerate(urls[1:], start=2):
        if url:
            cache[str(url).strip()] = idx
    _url_row_cache = cache
    return cache


def _fmt_date(value: Any) -> str:
    if not value:
        return ""
    s = str(value).strip()
    # SQLite CURRENT_TIMESTAMP → YYYY-MM-DD
    if " " in s:
        s = s.split(" ", 1)[0]
    return s


def _fmt_score(score: Any) -> str:
    if score is None or score == "":
        return ""
    try:
        return f"{float(score):.1f}"
    except (TypeError, ValueError):
        return str(score)


def _fmt_remote(remote: Any) -> str:
    if remote in (True, 1, "1", "yes", "true", "True"):
        return "Remote"
    if remote in (False, 0, "0", "no", "false", "False"):
        return "On-site / hybrid"
    return str(remote or "")


def _sheet_status(status: str | None) -> str:
    if not status:
        return STATUS_TO_SHEET["new"]
    return STATUS_TO_SHEET.get(status, status)


def _row_values(vacancy: dict[str, Any], *, preserve: list[str] | None = None) -> list[Any]:
    """Build a full row matching Jobs headers. Preserve package/follow-up/comment if updating."""
    preserve = preserve or []
    comment = str(vacancy.get("verdict") or vacancy.get("comment") or "")
    if len(comment) > 4500:
        comment = comment[:4500] + "…"

    package = vacancy.get("package")
    follow_up = vacancy.get("follow_up") or ""
    if preserve and len(preserve) >= 13:
        if package is None:
            package = preserve[8]
        if not follow_up:
            follow_up = preserve[11]
        # Keep human comment if bot verdict empty
        if not comment:
            comment = preserve[12]

    return [
        _fmt_date(vacancy.get("created_at")),
        vacancy.get("company") or "",
        vacancy.get("title") or "",
        vacancy.get("stack") or (preserve[3] if preserve and len(preserve) > 3 else ""),
        vacancy.get("salary") or "",
        _fmt_remote(vacancy.get("remote")) if vacancy.get("remote") is not None else (
            preserve[5] if preserve and len(preserve) > 5 else ""
        ),
        _fmt_score(vacancy.get("score")),
        vacancy.get("url") or "",
        package or "",
        _sheet_status(vacancy.get("status")),
        _fmt_date(vacancy.get("applied_at")),
        follow_up,
        comment,
    ]


def ensure_ready() -> bool:
    if not is_configured():
        return False
    with _lock:
        ws = _client_worksheet()
        _ensure_headers(ws)
        _rebuild_url_cache(ws)
    return True


def upsert_vacancy(vacancy: dict[str, Any]) -> bool:
    """Upsert с одним повтором при 429 (лимит Sheets API — 60 чтений/мин)."""
    for attempt in range(2):
        ok = _upsert_vacancy(vacancy)
        if ok is not None:
            return ok
        time.sleep(35)
    return False


def _upsert_vacancy(vacancy: dict[str, Any]) -> bool | None:
    global _next_row
    if not is_configured():
        return False
    url = str(vacancy.get("url") or "").strip()
    if not url:
        return False
    try:
        with _lock:
            ws = _client_worksheet()
            _ensure_headers(ws)
            fresh = _url_row_cache is not None and time.monotonic() - _cache_at < _CACHE_TTL
            cache = _url_row_cache if fresh else _rebuild_url_cache(ws)
            row_idx = cache.get(url)
            if row_idx:
                existing = ws.row_values(row_idx)
                # If caller only updates status fields, keep sheet status unless provided
                if vacancy.get("status") is None and len(existing) >= 10:
                    vacancy = {**vacancy, "status": SHEET_TO_STATUS.get(existing[9], existing[9])}
                values = _row_values(vacancy, preserve=existing)
                # Don't blank applied_at if not provided
                if not vacancy.get("applied_at") and len(existing) >= 11:
                    values[10] = existing[10]
                ws.update(f"A{row_idx}", [values], value_input_option="RAW")
            else:
                values = _row_values(vacancy)
                if _next_row is None:
                    _rebuild_url_cache(ws)
                next_row = _next_row
                if next_row > ws.row_count:
                    ws.add_rows(next_row - ws.row_count + 50)
                ws.update(
                    values=[values],
                    range_name=f"A{next_row}:M{next_row}",
                    value_input_option="RAW",
                )
                cache[url] = next_row
                _next_row = next_row + 1
            logger.info("sheets upsert url=%s status=%s", url[:80], values[9])
            return True
    except Exception as e:
        if "429" in str(e):
            logger.warning("sheets quota hit, retry later url=%s", url[:80])
            return None
        logger.exception("sheets upsert failed url=%s", url[:80])
        return False


def update_status(url: str, status: str, applied_at: str | None = None) -> bool:
    if not is_configured():
        return False
    url = str(url or "").strip()
    if not url:
        return False
    try:
        with _lock:
            ws = _client_worksheet()
            cache = _url_row_cache if _url_row_cache is not None else _rebuild_url_cache(ws)
            row_idx = cache.get(url)
            if not row_idx:
                logger.warning("sheets status skip, url not found: %s", url[:80])
                return False
            sheet_status = _sheet_status(status)
            ws.update_cell(row_idx, STATUS_COL, sheet_status)
            if applied_at is not None:
                ws.update_cell(row_idx, APPLIED_COL, _fmt_date(applied_at))
            logger.info("sheets status url=%s -> %s", url[:80], sheet_status)
            return True
    except Exception:
        logger.exception("sheets status failed url=%s", url[:80])
        return False


def reset_client() -> None:
    global _worksheet, _url_row_cache
    with _lock:
        _worksheet = None
        _url_row_cache = None
        globals()["_next_row"] = None
