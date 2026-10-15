from __future__ import annotations

import gzip
import io
import time
import urllib.error
import urllib.request
import zlib
from typing import Mapping

from .utils import UA, RateLimiter

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}


class FetchError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def fetch(
    url: str,
    *,
    limiter: RateLimiter,
    headers: Mapping[str, str] | None = None,
    retries: int = 4,
    timeout: float = 90,
    acceptable_status: tuple[int, ...] = (200,),
) -> tuple[bytes, Mapping[str, str]]:
    merged = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        merged.update(headers)

    last_error: Exception | None = None
    for attempt in range(retries):
        limiter.wait()
        request = urllib.request.Request(url, headers=merged)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                encoding = (response.headers.get("Content-Encoding") or "").lower()
                if encoding == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                elif encoding == "deflate":
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                if response.status not in acceptable_status:
                    raise FetchError(f"HTTP {response.status}: {url}", response.status)
                return raw, response.headers
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in RETRY_STATUS:
                raise FetchError(f"HTTP {exc.code}: {url}", exc.code) from exc
            time.sleep(min(30.0, 2 ** attempt * 1.5))
        except FetchError:
            raise
        except Exception as exc:
            last_error = exc
            time.sleep(min(30.0, 2 ** attempt * 1.5))
    raise FetchError(f"请求多次失败: {url} ({last_error})")


def fetch_text(url: str, **kwargs) -> str:
    raw, headers = fetch(url, **kwargs)
    charset = "utf-8"
    content_type = str(headers.get("Content-Type") or "")
    if "charset=" in content_type:
        charset = content_type.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
    try:
        return raw.decode(charset, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")
