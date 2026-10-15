from __future__ import annotations

import html as html_mod
import re
from typing import Callable

from .http import fetch_text
from .utils import BASE_URL, RateLimiter

_YEAR_ENTRY_RE = re.compile(
    r'<a href="/(\d{4})/(\d{3,5})">\1/\2</a>'
    r'(?:(?!<a href="/\d{4}/\d{3,5}">).)*?'
    r'<div class="papertitle">(.*?)</div>',
    re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")
_UPDATED_RE = re.compile(r"Last updated:\s*&nbsp;\s*([\d-]{10})")


def _clean(fragment: str) -> str:
    text = _TAG_RE.sub(" ", fragment)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_year_page(html: str, year: int) -> list[dict]:
    records: list[dict] = []
    seen: set[str] = set()
    for match in _YEAR_ENTRY_RE.finditer(html):
        paper_id = f"{match.group(1)}/{match.group(2)}"
        if paper_id in seen:
            continue
        seen.add(paper_id)
        title = _clean(match.group(3))
        tail = html[match.end(): match.end() + 400]
        updated = _UPDATED_RE.search(tail)
        records.append(
            {
                "paper_id": paper_id,
                "title": title or None,
                "authors": [],
                "categories": [],
                "abstract": None,
                "rights": None,
                "published": f"{year}-01-01",
                "updated": updated.group(1) if updated else None,
                "url": f"{BASE_URL}/{paper_id}",
                "pdf_url": f"{BASE_URL}/{paper_id}.pdf",
                "meta_source": "browse",
            }
        )
    return records


def parse_landing_page(html: str, paper_id: str) -> dict:
    meta: dict[str, list[str]] = {}
    for match in re.finditer(r'<meta name="(citation_[a-z_]+)" content="([^"]*)"', html):
        meta.setdefault(match.group(1), []).append(html_mod.unescape(match.group(2)))

    authors = meta.get("citation_author", [])
    if not authors:
        authors = [_clean(m) for m in re.findall(r'<meta name="DC\.creator" content="([^"]*)"', html)]

    abstract = None
    match = re.search(
        r'<div class="abstract[^"]*"[^>]*>(.*?)</div>\s*</div>', html, re.S
    ) or re.search(r'<p class="fst-italic[^"]*">(.*?)</p>', html, re.S)
    if match:
        text = _clean(match.group(1))
        text = re.sub(r"^Abstract\.?\s*", "", text)
        abstract = text or None

    categories = []
    for match in re.finditer(r'href="/search\?category=([^"&]+)"', html):
        value = html_mod.unescape(match.group(1)).replace("+", " ").strip()
        if value and value not in categories:
            categories.append(value)

    return {
        "paper_id": paper_id,
        "title": (meta.get("citation_title") or [None])[0],
        "authors": authors,
        "categories": categories,
        "abstract": abstract,
        "rights": None,
        "published": (meta.get("citation_publication_date") or [None])[0],
        "updated": (meta.get("citation_online_date") or [None])[0],
        "url": f"{BASE_URL}/{paper_id}",
        "pdf_url": f"{BASE_URL}/{paper_id}.pdf",
        "meta_source": "page",
    }


def harvest_year(
    year: int,
    *,
    limiter: RateLimiter,
    progress: Callable[[str], None] | None = None,
    max_pages: int = 200,
) -> list[dict]:
    records: list[dict] = []
    seen: set[str] = set()
    offset = 0
    for page in range(max_pages):
        url = f"{BASE_URL}/{year}/" if offset == 0 else f"{BASE_URL}/{year}/?offset={offset}"
        if progress:
            progress(f"  读取年度页 {url}")
        html = fetch_text(url, limiter=limiter)
        page_records = parse_year_page(html, year)
        fresh = [r for r in page_records if r["paper_id"] not in seen]
        for record in fresh:
            seen.add(record["paper_id"])
        records.extend(fresh)
        if len(page_records) < 100 or not fresh:
            break
        offset += 100
    return records


def enrich_paper(
    paper_id: str, *, limiter: RateLimiter
) -> dict | None:
    try:
        html = fetch_text(f"{BASE_URL}/{paper_id}", limiter=limiter)
    except Exception:
        return None
    parsed = parse_landing_page(html, paper_id)
    if not parsed.get("title") and not parsed.get("abstract") and not parsed.get("authors"):
        return None
    return parsed
