import html
import logging
import re

logger = logging.getLogger(__name__)


def clean_html(raw_html):
    """Очищает текст от HTML тегов, оставляя только разрешенные Telegram (b, i, code, a)."""
    if not raw_html:
        return ""

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw_html, "html.parser")
        allowed_tags = ["b", "i", "code", "a"]

        for tag in soup.find_all(True):
            if tag.name not in allowed_tags:
                tag.unwrap()
            else:
                allowed_attrs = ["href"] if tag.name == "a" else []
                tag.attrs = {k: v for k, v in tag.attrs.items() if k in allowed_attrs}

        return soup.decode_contents().strip()
    except Exception as e:
        logger.error("Error cleaning HTML: %s", e)
        return html.escape(re.sub(r"<.*?>", "", raw_html))
