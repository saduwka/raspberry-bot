import json
import logging
import re

logger = logging.getLogger(__name__)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _first_json_object(text):
    decoder = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
            if isinstance(obj, (dict, list)):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def extract_json(text):
    """Пытается извлечь JSON из ответа модели, исправляя типичные ошибки нейросетей."""
    if not text:
        return None
    raw = text
    try:
        text = _THINK_RE.sub("", text)
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        found = _first_json_object(text)
        if found is not None:
            return found
        cleaned = re.sub(
            r'(?<=: ")(.*?)(?=",?\n|(?:"\s*}))',
            lambda m: m.group(1).replace('"', '\\"').replace("\n", "\\n"),
            text,
            flags=re.DOTALL,
        )
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return _first_json_object(cleaned)

    except Exception as e:
        logger.warning("Failed to extract JSON. Error: %s. Raw text: %s...", e, raw[:200])
        return None
