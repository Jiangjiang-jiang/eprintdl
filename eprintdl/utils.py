from __future__ import annotations

import os
import re
import sys
import threading
import time
import unicodedata

BASE_URL = "https://eprint.iacr.org"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename_part(text: str, max_len: int = 120) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("&amp;", "and").replace("&lt;", "").replace("&gt;", "")
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    text = re.sub(r"[\s\u00a0]+", "_", text)
    text = re.sub(r'[\\/:*?"<>|\r\n\t]', "", text)
    text = re.sub(r"_{2,}", "_", text).strip("_.")
    if len(text) > max_len:
        text = text[:max_len].rstrip("_.")
    if text.upper() in _RESERVED:
        text = "_" + text
    return text or "untitled"


def paper_stem(paper_id: str, title: str, max_len: int = 120) -> str:
    year, _, number = paper_id.partition("/")
    base = f"{year}-{number}"
    title_part = sanitize_filename_part(title, max_len=max_len)
    stem = f"{base}_{title_part}"
    return stem[:max_len + 12].rstrip("_.")


def normalize_paper_id(raw: str) -> str | None:
    raw = raw.strip()
    m = re.search(r"(\d{4})\s*[/\-_]\s*(\d{1,5})", raw)
    if not m:
        m = re.search(r"^(\d{4})(\d{3,5})$", raw)
    if not m:
        return None
    return f"{m.group(1)}/{int(m.group(2)):03d}"


def pdf_url(paper_id: str, base: str = BASE_URL) -> str:
    return f"{base}/{paper_id}.pdf"


def landing_url(paper_id: str, base: str = BASE_URL) -> str:
    return f"{base}/{paper_id}"


class RateLimiter:

    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, delay)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = self.delay - (now - self._last)
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last = time.monotonic()


def human_bytes(n: int | float | None) -> str:
    if not n:
        return "0 B"
    step = 1024.0
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < step:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TB"


def log(message: str, *, quiet: bool = False) -> None:
    if not quiet:
        print(message, flush=True)


def err(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path
