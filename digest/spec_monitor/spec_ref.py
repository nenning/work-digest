"""Normalisation of a user-given spec reference (title, page id or Confluence URL)."""
import re
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

UPDATE_ALL = "*"   # --update-spec without a value: every analysed spec

_PAGE_PATH = re.compile(r"/pages/(\d+)(?:/|$)")


def normalize_spec_ref(value: Optional[str]) -> Optional[str]:
    """Confluence page URL -> page id; anything else (title, id) is returned stripped.

    Handles /wiki/spaces/KEY/pages/<id>/<title>, /wiki/pages/viewpage.action?pageId=<id>
    and /wiki/x/... is not resolvable and therefore left as given.
    """
    if value is None:
        return None
    text = value.strip()
    if not re.match(r"https?://", text, re.I):
        return text
    parsed = urlparse(text)
    m = _PAGE_PATH.search(unquote(parsed.path))
    if m:
        return m.group(1)
    page_id = parse_qs(parsed.query).get("pageId")
    if page_id and page_id[0].isdigit():
        return page_id[0]
    raise ValueError(f"Cannot find a Confluence page id in the URL: {text}")
