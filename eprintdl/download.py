from __future__ import annotations

import base64
import hashlib
import os
import threading
import time
from typing import Callable

from .cdp import CdpConnection, CdpError, Chrome
from .platform import resolve_browser
from .utils import BASE_URL, RateLimiter, paper_stem, pdf_url

PDF_MAGIC = b"%PDF-"
CHALLENGE_MARKERS = (b"Just a moment", b"challenge-platform", b"cf-mitigated", b"__cf_chl")
PDF_PATTERNS = [
    {"urlPattern": "*.pdf", "requestStage": "Response"},
    {"urlPattern": "*.pdf?*", "requestStage": "Response"},
]


class DownloadError(RuntimeError):
    pass


class ChallengeError(DownloadError):
    pass


class PdfResult:
    __slots__ = ("paper_id", "path", "size", "sha256", "elapsed", "attempts", "reused")

    def __init__(
        self,
        paper_id: str,
        path: str,
        size: int,
        sha256: str,
        elapsed: float,
        attempts: int,
        reused: bool = False,
    ) -> None:
        self.paper_id = paper_id
        self.path = path
        self.size = size
        self.sha256 = sha256
        self.elapsed = elapsed
        self.attempts = attempts
        self.reused = reused


class _Interceptor:

    def __init__(self, conn: CdpConnection) -> None:
        self.conn = conn
        self._lock = threading.RLock()
        self._events: dict[str, threading.Event] = {}
        self._payload: dict[str, dict] = {}
        self.enabled = False

    def enable(self) -> None:
        with self._lock:
            self.conn.call("Fetch.enable", {"patterns": PDF_PATTERNS})
            self.enabled = True

    def disable(self) -> None:
        with self._lock:
            try:
                self.conn.call("Fetch.disable")
            except Exception:
                pass
            self.enabled = False

    def arm(self, url: str) -> threading.Event:
        with self._lock:
            event = threading.Event()
            self._events[url] = event
            self._payload.pop(url, None)
        return event

    def result(self, url: str) -> dict | None:
        with self._lock:
            return self._payload.get(url)

    def handle(self, message: dict) -> None:
        if message.get("method") != "Fetch.requestPaused":
            return
        params = message["params"]
        request_id = params.get("requestId")
        url = params.get("request", {}).get("url", "")
        status = params.get("responseStatusCode")
        with self._lock:
            interesting = url in self._events
            enabled = self.enabled
        if not interesting or status is None or not enabled:
            self._continue(request_id)
            return

        payload: dict = {"status": status}
        try:
            body = self.conn.call("Fetch.getResponseBody", {"requestId": request_id}, timeout=300)
            if body.get("base64Encoded"):
                payload["data"] = base64.b64decode(body["body"])
            else:
                payload["data"] = (body.get("body") or "").encode("utf-8", "replace")
        except Exception as exc:
            payload["error"] = repr(exc)
        with self._lock:
            self._payload[url] = payload
            event = self._events.get(url)
        if event:
            event.set()
        self._continue(request_id)

    def _continue(self, request_id: str | None) -> None:
        if not request_id:
            return
        try:
            self.conn.call("Fetch.continueRequest", {"requestId": request_id}, timeout=60)
        except Exception:
            pass


class BrowserDownloader:

    def __init__(
        self,
        *,
        chrome_path: str | None = None,
        profile_dir: str,
        port: int = 9222,
        headless: bool = False,
        limiter: RateLimiter | None = None,
        page_timeout: float = 120.0,
        base_url: str = BASE_URL,
        verbose: bool = True,
        on_message: Callable[[str], None] | None = None,
        warmup: bool = True,
        needs_windows_path: bool = False,
    ) -> None:
        self.chrome_path = chrome_path or resolve_browser()[0]
        self.profile_dir = profile_dir
        self.port = port
        self.headless = headless
        self.limiter = limiter or RateLimiter(1.0)
        self.page_timeout = page_timeout
        self.base_url = base_url
        self.verbose = verbose
        self.on_message = on_message
        self.warmup = warmup
        self.needs_windows_path = needs_windows_path
        self.chrome: Chrome | None = None
        self.conn: CdpConnection | None = None
        self.interceptor: _Interceptor | None = None
        self._session_ready = False
        self._nav_lock = threading.RLock()

    def __enter__(self) -> "BrowserDownloader":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _say(self, message: str) -> None:
        if self.verbose and self.on_message:
            self.on_message(message)

    def start(self) -> None:
        if not self.chrome_path:
            raise DownloadError(
                "找不到浏览器。请安装 Chrome/Chromium/Edge，或用 --chrome 指定路径"
                "（也可设环境变量 EPRINTDL_CHROME）。"
            )
        try:
            self.chrome = Chrome(
                self.chrome_path,
                self.profile_dir,
                port=self.port,
                headless=self.headless,
                log_path=os.path.join(self.profile_dir, "chrome.log"),
                needs_windows_path=self.needs_windows_path,
            )
            self.conn = self.chrome.start()
        except CdpError as exc:
            raise DownloadError(f"启动浏览器失败: {exc}") from exc
        self.conn.call("Network.enable")
        self.interceptor = _Interceptor(self.conn)
        self.conn.on_event(self.interceptor.handle)
        try:
            self.conn.call("Browser.setDownloadBehavior", {"behavior": "deny"})
        except Exception:
            pass

    def stop(self) -> None:
        if self.chrome:
            self.chrome.stop()
            self.chrome = None
            self.conn = None
            self.interceptor = None

    def warm_up(self) -> None:
        if self._session_ready:
            return
        assert self.conn is not None
        self._say("  预热会话（访问站点首页）...")
        try:
            self.conn.call(
                "Page.navigate", {"url": f"{self.base_url}/"}, timeout=self.page_timeout
            )
        except Exception:
            pass
        time.sleep(2.0)
        self._session_ready = True

    def _navigate(self, url: str) -> None:
        assert self.conn is not None
        with self._nav_lock:
            self.conn.call("Page.navigate", {"url": url}, timeout=self.page_timeout)

    def _reset_page(self) -> None:
        try:
            self._navigate("about:blank")
        except Exception:
            pass

    def _page_state(self) -> dict:
        assert self.conn is not None
        try:
            raw = self.conn.call(
                "Runtime.evaluate",
                {
                    "expression": (
                        "JSON.stringify({ct: document.contentType || '',"
                        " title: document.title || '',"
                        " head: (document.documentElement ? document.documentElement.outerHTML : '').slice(0, 6000)})"
                    ),
                    "returnByValue": True,
                },
                timeout=10,
            ).get("result", {}).get("value", "")
        except Exception:
            return {}
        if not raw:
            return {}
        try:
            import json as _json

            state = _json.loads(raw)
        except Exception:
            return {}
        state["challenge"] = any(
            marker in state.get("head", "")
            for marker in ("challenge-platform", "Just a moment", "cf-mitigated", "__cf_chl")
        ) or "稍候" in state.get("title", "")
        return state

    def _cookies(self) -> dict[str, str]:
        assert self.conn is not None
        try:
            found = self.conn.call(
                "Network.getCookies", {"urls": [f"{self.base_url}/"]}, timeout=10
            ).get("cookies", [])
        except Exception:
            return {}
        return {c.get("name"): c.get("value") for c in found if c.get("name")}

    def clear_clearance(self) -> None:
        assert self.conn is not None
        try:
            cookies = self.conn.call(
                "Network.getCookies", {"urls": [f"{self.base_url}/"]}, timeout=10
            ).get("cookies", [])
        except Exception:
            return
        for cookie in cookies:
            try:
                self.conn.call(
                    "Network.deleteCookies",
                    {
                        "name": cookie["name"],
                        "domain": cookie.get("domain"),
                        "path": cookie.get("path"),
                    },
                    timeout=10,
                )
            except Exception:
                pass

    def _fetch_body(self, url: str, timeout: float) -> dict:
        assert self.interceptor is not None
        event = self.interceptor.arm(url)
        try:
            self._navigate(url)
        except Exception as exc:
            return {"error": repr(exc)}
        waited = 0.0
        while not event.wait(min(15.0, timeout - waited)):
            waited += 15.0
            if waited >= timeout:
                return {"error": f"等待响应超时（{timeout:.0f}s）"}
            self._say(f"  仍在等待站点响应（已等 {waited:.0f} 秒，{timeout:.0f} 秒后放弃并重试）...")
        return self.interceptor.result(url) or {"error": "没有拿到响应体"}

    @staticmethod
    def _is_challenge(payload: dict) -> bool:
        data = payload.get("data") or b""
        if payload.get("status") in (403, 503) and any(m in data for m in CHALLENGE_MARKERS):
            return True
        return any(m in data for m in CHALLENGE_MARKERS) and data[:5] != PDF_MAGIC

    def _solve_challenge(self, url: str, attempt: int, fresh: bool = False) -> bool:
        assert self.interceptor is not None and self.conn is not None
        self._say(f"  遇到 Cloudflare 质询，交还浏览器处理（第 {attempt} 次）...")
        self.interceptor.disable()
        if fresh:
            self._say("  清除站点 cookie 后重新过质询...")
            self.clear_clearance()
        try:
            self._navigate(url)
        except Exception:
            pass

        deadline = time.time() + 60
        cleared = False
        polls = 0
        while time.time() < deadline:
            time.sleep(0.5)
            polls += 1
            state = self._page_state()
            if state.get("ct") == "application/pdf":
                cleared = True
                break
            if polls >= 3 and state and not state.get("challenge"):
                cleared = True
                break
        self._say("  质询已通过。" if cleared else "  质询未在 60 秒内完成，继续尝试。")
        self.interceptor.enable()
        self._reset_page()
        return cleared

    def download_pdf(
        self,
        paper_id: str,
        dest_path: str,
        *,
        attempts: int = 3,
        validate: bool = True,
    ) -> PdfResult:
        if self.conn is None or self.interceptor is None:
            raise DownloadError("下载器尚未启动")
        url = pdf_url(paper_id, self.base_url)
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        self.warm_up()
        self.interceptor.enable()
        self._session_ready = True
        last_error: Exception | None = None
        challenge_count = 0

        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            self.limiter.wait()
            payload = self._fetch_body(url, self.page_timeout)

            if "error" in payload:
                last_error = DownloadError(str(payload["error"]))
                self._reset_page()
                continue

            data: bytes = payload.get("data", b"")
            status = payload.get("status")

            if data[:5] == PDF_MAGIC:
                if validate and b"%%EOF" not in data[-4096:]:
                    last_error = DownloadError("PDF 看似被截断（结尾缺少 %%EOF）")
                    self._reset_page()
                    continue
                digest = hashlib.sha256(data).hexdigest()
                tmp_path = dest_path + ".part"
                with open(tmp_path, "wb") as handle:
                    handle.write(data)
                os.replace(tmp_path, dest_path)
                return PdfResult(
                    paper_id=paper_id,
                    path=dest_path,
                    size=len(data),
                    sha256=digest,
                    elapsed=time.monotonic() - started,
                    attempts=attempt,
                )

            if self._is_challenge(payload):
                challenge_count += 1
                last_error = ChallengeError(f"被 Cloudflare 质询拦截（HTTP {status}）")
                self._solve_challenge(url, attempt, fresh=challenge_count > 1)
                continue

            snippet = data[:120].decode("utf-8", "replace").replace("\n", " ")
            last_error = DownloadError(f"响应不是 PDF（HTTP {status}）: {snippet}")
            self._reset_page()
            if status in (404, 410):
                raise DownloadError(f"{paper_id} 不存在或已撤回（HTTP {status}）")

        raise DownloadError(f"{paper_id} 下载失败: {last_error}")

    def fetch_one(
        self,
        paper_id: str,
        title: str | None,
        *,
        out_dir: str,
        attempts: int = 3,
        max_title_len: int = 120,
    ) -> PdfResult:
        stem = paper_stem(paper_id, title or "", max_len=max_title_len)
        dest = os.path.join(out_dir, stem + ".pdf")
        index = 1
        while os.path.exists(dest):
            size = os.path.getsize(dest)
            if size > 1024:
                with open(dest, "rb") as handle:
                    head = handle.read(5)
                if head == PDF_MAGIC:
                    if index == 1:
                        digest = hashlib.sha256()
                        with open(dest, "rb") as handle:
                            for chunk in iter(lambda: handle.read(1 << 20), b""):
                                digest.update(chunk)
                        return PdfResult(
                            paper_id, dest, size, digest.hexdigest(), 0.0, 0, reused=True
                        )
                    break
            index += 1
            dest = os.path.join(out_dir, f"{stem}-{index}.pdf")
        return self.download_pdf(paper_id, dest, attempts=attempts)
