import json
import logging
import re

logger = logging.getLogger(__name__)


def extract_json(text):
    """Пытается извлечь JSON из ответа модели, исправляя типичные ошибки нейросетей."""
    try:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            cleaned = re.sub(
                r'(?<=: ")(.*?)(?=",?\n|(?:"\s*}))',
                lambda m: m.group(1).replace('"', '\\"').replace("\n", "\\n"),
                text,
                flags=re.DOTALL,
            )
            return json.loads(cleaned)

    except Exception as e:
        logger.warning("Failed to extract JSON. Error: %s. Raw text: %s...", e, text[:200])
        return None
