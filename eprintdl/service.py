from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .cdp import CdpError
from .config import Config
from .download import BrowserDownloader, ChallengeError, DownloadError
from .locking import ChromeLock, LockTimeout
from .platform import browser_preflight, resolve_browser
from .reporter import Reporter
from .store import Store
from .utils import human_bytes, normalize_paper_id, paper_stem

EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_USAGE = 2
EXIT_LOCK_TIMEOUT = 3


@dataclass
class PaperReport:

    paper_id: str
    status: str
    file_path: str | None = None
    size: int | None = None
    sha256: str | None = None
    elapsed: float = 0.0
    attempts: int = 0
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "paper_id": self.paper_id,
            "status": self.status,
            "file_path": os.path.abspath(self.file_path) if self.file_path else None,
            "size": self.size,
            "size_human": human_bytes(self.size) if self.size else None,
            "sha256": self.sha256,
            "elapsed": round(self.elapsed, 2),
            "attempts": self.attempts,
            "error": self.error,
        }


@dataclass
class DownloadOutcome:
    ok: bool
    total: int = 0
    downloaded: int = 0
    reused: int = 0
    failed: int = 0
    elapsed: float = 0.0
    exit_code: int = EXIT_OK
    results: list[PaperReport] = field(default_factory=list)
    lock_wait_note: str | None = None

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "downloaded": self.downloaded,
            "reused": self.reused,
            "failed": self.failed,
            "elapsed": round(self.elapsed, 2),
            "exit_code": self.exit_code,
            "results": [item.as_dict() for item in self.results],
        }


def register_ids(store: Store, ids: Iterable[str], base_url: str | None = None) -> list[str]:
    from .utils import BASE_URL

    known = store.known_ids()
    fresh = [paper_id for paper_id in ids if paper_id not in known]
    if fresh:
        store.upsert_metadata(
            [
                {
                    "paper_id": paper_id,
                    "title": None,
                    "meta_source": "manual",
                    "url": f"{base_url or BASE_URL}/{paper_id}",
                    "pdf_url": f"{base_url or BASE_URL}/{paper_id}.pdf",
                }
                for paper_id in fresh
            ]
        )
    return fresh


def normalize_ids(raw: Iterable[str]) -> list[str]:
    ids: list[str] = []
    for token in raw:
        paper_id = normalize_paper_id(str(token))
        if paper_id and paper_id not in ids:
            ids.append(paper_id)
    return ids


def read_id_file(path: str) -> list[str]:
    ids: list[str] = []
    with open(path, "r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            token = line.replace(",", " ").split()[0]
            paper_id = normalize_paper_id(token)
            if paper_id:
                ids.append(paper_id)
    return ids


def download_papers(
    config: Config,
    store: Store,
    paper_ids: Sequence[str],
    *,
    reporter: Reporter,
    limiter,
    retries: int | None = None,
    title_len: int | None = None,
    need_title: bool = True,
    lock_timeout: float = 120.0,
    use_lock: bool = True,
    port: int | None = None,
) -> DownloadOutcome:
    from .browse import enrich_paper
    from .http import FetchError

    retries = retries if retries is not None else config.retries
    title_len = title_len if title_len is not None else config.title_len
    ids = normalize_ids(paper_ids)
    if not ids:
        return DownloadOutcome(ok=False, exit_code=EXIT_USAGE)

    register_ids(store, ids, base_url=None)
    os.makedirs(config.out, exist_ok=True)

    rows = {row["paper_id"]: row for row in store.query(ids=ids, limit=None)}
    pending = [rows[paper_id] for paper_id in ids if paper_id in rows]

    if need_title:
        missing = [row for row in pending if not row["title"]]
        if missing:
            reporter.say(f"有 {len(missing)} 篇缺标题，先抓取标题用于命名...")
            for index, row in enumerate(missing, 1):
                try:
                    parsed = enrich_paper(row["paper_id"], limiter=limiter)
                except FetchError:
                    parsed = None
                if parsed:
                    store.upsert_metadata([parsed])
                if index % 20 == 0 or index == len(missing):
                    reporter.say(f"  标题补齐 {index}/{len(missing)}")
            rows = {row["paper_id"]: row for row in store.query(ids=ids, limit=None)}
            pending = [rows[paper_id] for paper_id in ids if paper_id in rows]

    outcome = DownloadOutcome(ok=True, total=len(pending))
    if not pending:
        return outcome

    reporter.say(
        f"准备下载 {len(pending)} 篇 -> {config.out}\n"
        f"命名：<年份>-<编号>_<无空格标题>.pdf；限速 {config.rate}s；单篇重试 {retries} 次"
    )

    browser_path, needs_windows_path = resolve_browser(config.chrome)
    if not browser_path:
        outcome.ok = False
        outcome.failed = 1
        outcome.exit_code = EXIT_USAGE
        outcome.results.append(
            PaperReport(
                "",
                "failed",
                error=(
                    "找不到浏览器。请安装 Chrome/Chromium/Edge，或用 --chrome 指定路径"
                    "（也可设环境变量 EPRINTDL_CHROME）。"
                ),
            )
        )
        return outcome
    if needs_windows_path:
        reporter.say(
            "  提示：使用 Windows 版浏览器（WSL 场景），路径参数会自动转换为 Windows 形式。"
        )
    problems = browser_preflight(browser_path, needs_windows_path, config.profile)
    if problems:
        outcome.ok = False
        outcome.failed = 1
        outcome.exit_code = EXIT_USAGE
        outcome.results.append(PaperReport("", "failed", error=" ".join(problems)))
        reporter.warn("无法启动浏览器：" + " ".join(problems))
        return outcome
    started_all = time.monotonic()

    def run() -> None:
        with BrowserDownloader(
            chrome_path=browser_path,
            profile_dir=config.profile,
            port=port or config.port or 9222,
            headless=config.headless,
            limiter=limiter,
            page_timeout=config.timeout,
            verbose=True,
            on_message=reporter.say,
            needs_windows_path=needs_windows_path,
            window=config.window,
        ) as downloader:
            for index, row in enumerate(pending, 1):
                paper_id = row["paper_id"]
                store.bump_attempt(paper_id)
                progress = f"[{index}/{len(pending)}] {index / len(pending) * 100:5.1f}%"
                try:
                    result = downloader.fetch_one(
                        paper_id,
                        row["title"],
                        out_dir=config.out,
                        attempts=retries,
                        max_title_len=title_len,
                    )
                except ChallengeError as exc:
                    report = PaperReport(paper_id, "failed", error=str(exc))
                    store.mark_failed(paper_id, str(exc))
                    outcome.results.append(report)
                    outcome.failed += 1
                    reporter.event("paper", **report.as_dict())
                    reporter.warn(f"{progress} {paper_id} 被质询拦截: {exc}")
                    continue
                except Exception as exc:
                    detail = (
                        str(exc)
                        if isinstance(exc, DownloadError)
                        else f"{type(exc).__name__}: {exc}"
                    )
                    report = PaperReport(paper_id, "failed", error=detail)
                    store.mark_failed(paper_id, detail)
                    outcome.results.append(report)
                    outcome.failed += 1
                    reporter.event("paper", **report.as_dict())
                    reporter.warn(f"{progress} {paper_id} 失败: {detail}")
                    continue

                store.mark_done(paper_id, result.path, result.size, result.sha256)
                reused = result.attempts == 0
                report = PaperReport(
                    paper_id=paper_id,
                    status="reused" if reused else "done",
                    file_path=result.path,
                    size=result.size,
                    sha256=result.sha256,
                    elapsed=result.elapsed,
                    attempts=result.attempts,
                )
                outcome.results.append(report)
                if reused:
                    outcome.reused += 1
                else:
                    outcome.downloaded += 1
                reporter.event("paper", **report.as_dict())
                if reused:
                    reporter.say(f"{progress} {paper_id} 已存在，跳过")
                else:
                    reporter.say(
                        f"{progress} {paper_id} OK {result.elapsed:.1f}s "
                        f"{human_bytes(result.size)} {os.path.basename(result.path)}"
                    )

    try:
        if use_lock:
            with ChromeLock(
                config.lock,
                timeout=lock_timeout,
                port=port or config.port or None,
                message=reporter.say,
            ) as lock:
                run()
                outcome.lock_wait_note = f"port={lock.port}"
        else:
            run()
    except LockTimeout as exc:
        outcome.ok = False
        outcome.exit_code = EXIT_LOCK_TIMEOUT
        outcome.results.append(PaperReport("", "failed", error=str(exc)))
        return outcome
    except (DownloadError, CdpError, TimeoutError, OSError) as exc:
        detail = f"启动浏览器失败: {exc}"
        outcome.ok = False
        outcome.failed += 1
        outcome.exit_code = EXIT_PARTIAL_FAILURE
        outcome.results.append(PaperReport("", "failed", error=detail))
        reporter.warn(detail)
        return outcome

    outcome.elapsed = time.monotonic() - started_all
    outcome.exit_code = EXIT_OK if outcome.failed == 0 else EXIT_PARTIAL_FAILURE
    outcome.ok = outcome.failed == 0
    return outcome


def retry_failed(
    config: Config,
    store: Store,
    *,
    reporter: Reporter,
    limiter,
    lock_timeout: float = 120.0,
    **kwargs,
) -> DownloadOutcome:
    rows = store.query(status=["failed"], limit=None, order="oldest")
    ids = [row["paper_id"] for row in rows]
    if not ids:
        return DownloadOutcome(ok=True, total=0)
    for paper_id in ids:
        store.mark_pending(paper_id)
    return download_papers(
        config, store, ids, reporter=reporter, limiter=limiter,
        lock_timeout=lock_timeout, **kwargs,
    )


@dataclass
class VerifyOutcome:
    ok: bool
    checked: int = 0
    broken: list[dict] = field(default_factory=list)
    exit_code: int = EXIT_OK

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "broken_count": len(self.broken),
            "broken": self.broken,
            "exit_code": self.exit_code,
        }


def verify_files(
    store: Store,
    *,
    years: list[int] | None = None,
    limit: int | None = None,
    reporter: Reporter | None = None,
) -> VerifyOutcome:
    rows = store.query(years=years, status=["done"], limit=limit, order="oldest")
    outcome = VerifyOutcome(ok=True, checked=len(rows))
    for index, row in enumerate(rows, 1):
        path = row["file_path"]
        problem = None
        if not path or not os.path.exists(path):
            problem = "文件不存在"
        else:
            size = os.path.getsize(path)
            if size < 1024:
                problem = f"文件过小（{size} 字节）"
            else:
                with open(path, "rb") as handle:
                    head = handle.read(5)
                    handle.seek(max(0, size - 4096))
                    tail = handle.read()
                if head != b"%PDF-":
                    problem = "缺少 PDF 文件头"
                elif b"%%EOF" not in tail:
                    problem = "缺少 PDF 结束标记"
        if problem:
            record = {
                "paper_id": row["paper_id"],
                "file_path": os.path.abspath(path) if path else None,
                "problem": problem,
            }
            outcome.broken.append(record)
            store.mark_failed(row["paper_id"], f"校验失败: {problem}")
            if reporter:
                reporter.event("broken", **record)
                reporter.warn(f"  损坏 {row['paper_id']} {problem}")
        if reporter and index % 200 == 0:
            reporter.say(f"  校验 {index}/{len(rows)}")
    outcome.ok = not outcome.broken
    outcome.exit_code = EXIT_OK if outcome.ok else EXIT_PARTIAL_FAILURE
    return outcome


def search_papers(
    store: Store,
    *,
    keyword: str | None = None,
    years: list[int] | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    categories: list[str] | None = None,
    status: list[str] | None = None,
    downloaded_only: bool = False,
    limit: int = 50,
    order: str = "newest",
) -> list[dict]:
    rows = store.query(
        keyword=keyword,
        years=years,
        year_from=year_from,
        year_to=year_to,
        categories=categories,
        status=status,
        limit=limit,
        order=order,
    )
    import json as _json

    out: list[dict] = []
    for row in rows:
        path = row["file_path"]
        exists = bool(path) and os.path.exists(path)
        if downloaded_only and not exists:
            continue
        out.append(
            {
                "paper_id": row["paper_id"],
                "title": row["title"],
                "authors": _json.loads(row["authors"] or "[]"),
                "categories": _json.loads(row["categories"] or "[]"),
                "published": row["published"],
                "updated": row["updated"],
                "url": row["url"],
                "pdf_url": row["pdf_url"],
                "status": row["status"],
                "file_path": os.path.abspath(path) if path and exists else None,
                "file_exists": exists,
                "file_size": row["file_size"],
            }
        )
    return out


def status_snapshot(store: Store, config: Config) -> dict:
    counts = store.counts()
    years = []
    for year in store.years_present():
        rows = store.query(years=[year], limit=None)
        years.append(
            {
                "year": year,
                "records": len(rows),
                "downloaded": sum(1 for row in rows if row["status"] == "done"),
                "failed": sum(1 for row in rows if row["status"] == "failed"),
                "latest_number": max((row["number"] for row in rows), default=0),
                "listing_baseline": store.get_meta(f"browse.count.{year}"),
            }
        )
    files = 0
    bytes_total = 0
    if os.path.isdir(config.out):
        for name in os.listdir(config.out):
            if name.lower().endswith(".pdf"):
                files += 1
                try:
                    bytes_total += os.path.getsize(os.path.join(config.out, name))
                except OSError:
                    pass
    return {
        "config": config.as_dict(),
        "db": os.path.abspath(config.db),
        "out": os.path.abspath(config.out),
        "totals": {
            "records": counts.get("total", 0),
            "downloaded": counts.get("done", 0),
            "failed": counts.get("failed", 0),
            "pending": counts.get("pending", 0),
            "years": counts.get("years", 0),
            "pdf_files_on_disk": files,
            "pdf_bytes_on_disk": bytes_total,
        },
        "years": years,
    }
