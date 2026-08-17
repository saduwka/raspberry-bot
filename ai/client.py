import logging
import time

import httpx

from config import LOCAL_LLM_API_KEY, LOCAL_LLM_BASE_URL, LOCAL_LLM_MODEL, LOCAL_LLM_TIMEOUT

logger = logging.getLogger(__name__)

last_error: str | None = None
last_ok_at: float | None = None
last_latency_ms: int | None = None

_client: httpx.AsyncClient | None = None


def model_label() -> str:
    return f"local/{LOCAL_LLM_MODEL}"


def _origin() -> str:
    base = (LOCAL_LLM_BASE_URL or "").rstrip("/")
    if base.endswith("/v1"):
        return base[:-3]
    return base


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if LOCAL_LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LOCAL_LLM_API_KEY}"
    return headers


async def startup() -> None:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=LOCAL_LLM_TIMEOUT, headers=_headers())


async def shutdown() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _get_client() -> httpx.AsyncClient:
    if _client is None:
        await startup()
    return _client


async def health() -> tuple[bool, str]:
    global last_error
    url = f"{_origin()}/health"
    try:
        client = await _get_client()
        response = await client.get(url, timeout=8.0)
        if response.status_code == 200:
            return True, response.text.strip()[:200]
        last_error = f"health HTTP {response.status_code}: {response.text[:200]}"
        logger.error("AI local failed: %s", last_error)
        return False, last_error
    except Exception as exc:
        last_error = f"health failed: {exc}"
        logger.error("AI local failed: %s", last_error)
        return False, last_error


async def chat(prompt: str, timeout: float | None = None) -> str | None:
    global last_error, last_ok_at, last_latency_ms
    started = time.monotonic()
    url = f"{(LOCAL_LLM_BASE_URL or '').rstrip('/')}/chat/completions"
    payload = {
        "model": LOCAL_LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }
    try:
        client = await _get_client()
        response = await client.post(url, json=payload, timeout=timeout or LOCAL_LLM_TIMEOUT)
        last_latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code != 200:
            last_error = f"chat HTTP {response.status_code}: {response.text[:300]}"
            logger.error("AI local failed: %s", last_error)
            return None
        data = response.json()
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        if not text or not str(text).strip():
            last_error = "empty content"
            logger.error("AI local failed: empty content")
            return None
        last_error = None
        last_ok_at = time.time()
        logger.info(
            "AI provider=local model=%s latency_ms=%s",
            LOCAL_LLM_MODEL,
            last_latency_ms,
        )
        return str(text).strip()
    except Exception as exc:
        last_latency_ms = int((time.monotonic() - started) * 1000)
        last_error = str(exc)
        logger.error("AI local failed: %s", last_error)
        return None


async def ping() -> tuple[bool, str]:
    ok, health_text = await health()
    if not ok:
        return False, health_text
    text = await chat("Reply with exactly: ok", timeout=45)
    if text:
        return True, text[:300]
    return False, last_error or "локальная модель недоступна"


def status_text() -> str:
    error = last_error or "нет"
    latency = f"{last_latency_ms} ms" if last_latency_ms is not None else "—"
    return (
        f"🤖 <b>Local LLM</b>\n"
        f"Модель: <code>{model_label()}</code>\n"
        f"URL: <code>{LOCAL_LLM_BASE_URL}</code>\n"
        f"Последняя задержка: <code>{latency}</code>\n"
        f"Последняя ошибка: <code>{error}</code>"
    )
