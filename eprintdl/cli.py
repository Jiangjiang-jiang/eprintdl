from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Iterable, Sequence

from . import __version__
from .browse import enrich_paper, harvest_year
from .config import Config, load_config, write_default_config
from .http import FetchError
from .oai import OaiClient, OaiError
from .locking import ChromeLock, LockTimeout
from .platform import browser_preflight, describe_platform, resolve_browser, wsl_networking_mode
from .reporter import Reporter, resolve_mode
from .service import (
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    EXIT_USAGE,
    download_papers,
    normalize_ids,
    read_id_file,
    register_ids,
    retry_failed,
    search_papers,
    status_snapshot,
    verify_files,
)
from .store import Store
from .utils import BASE_URL, RateLimiter, UA, human_bytes, paper_stem


def parse_years(values: Sequence[str] | None) -> list[int]:
    years: list[int] = []
    for item in values or []:
        for part in str(item).replace(",", " ").split():
            if "-" in part:
                start, _, end = part.partition("-")
                if start.strip().isdigit() and end.strip().isdigit():
                    years += list(range(int(start), int(end) + 1))
                    continue
            if part.strip().isdigit():
                years.append(int(part))
    return sorted(set(years))


def build_config(args: argparse.Namespace) -> Config:
    overrides = {
        "db": getattr(args, "db", None),
        "out": getattr(args, "out", None),
        "profile": getattr(args, "profile", None),
        "rate": getattr(args, "rate", None),
        "retries": getattr(args, "retries", None),
        "timeout": getattr(args, "timeout", None),
        "title_len": getattr(args, "title_len", None),
        "chrome": getattr(args, "chrome", None) or None,
        "port": getattr(args, "port", None),
        "headless": True if getattr(args, "headless", False) else None,
    }
    return load_config(getattr(args, "config", None), overrides=overrides)


def make_reporter(args: argparse.Namespace) -> Reporter:
    return Reporter(resolve_mode(args))


def collect_ids(args: argparse.Namespace) -> list[str]:
    raw: list[str] = []
    for item in getattr(args, "ids", None) or []:
        raw += str(item).replace(",", " ").split()
    if getattr(args, "id_file", None):
        raw += read_id_file(args.id_file)
    return normalize_ids(raw)


def cmd_doctor(args: argparse.Namespace) -> int:
    reporter = make_reporter(args)
    config = build_config(args)
    problems = 0
    report: dict = {"python": sys.version.split()[0], "executable": sys.executable}

    reporter.say(f"Python      : {report['python']} ({report['executable']})")
    if sys.version_info < (3, 10):
        problems += 1
        report["python_ok"] = False
        reporter.warn("  问题：需要 Python 3.10 以上")
    else:
        report["python_ok"] = True
        reporter.say("  通过：版本满足要求")

    platform_info = describe_platform()
    report["platform"] = platform_info
    reporter.say(
        f"运行平台    : {platform_info['os']}"
        + ("（WSL）" if platform_info["in_wsl"] else "")
    )
    for note in platform_info["notes"]:
        reporter.say(f"  - {note}")

    browser, needs_windows_path = resolve_browser(config.chrome)
    report["browser"] = browser
    report["browser_needs_windows_path"] = needs_windows_path
    if browser:
        kind = "Windows 程序，经 WSL interop 调用" if needs_windows_path else "本机程序"
        reporter.say(f"浏览器      : {browser}（{kind}）")
        if platform_info["in_wsl"]:
            reporter.say(f"  WSL 网络模式: {wsl_networking_mode()}")
        preflight = browser_preflight(browser, needs_windows_path, config.profile)
        report["browser_preflight"] = preflight
        for problem in preflight:
            problems += 1
            reporter.warn(f"  问题：{problem}")
        if not preflight:
            launch_ok, detail = _browser_launch_check(config, browser, needs_windows_path)
            report["browser_launch"] = {"ok": launch_ok, "detail": detail}
            if launch_ok:
                reporter.say(f"  通过：浏览器可启动并接受控制（{detail}）")
            elif launch_ok is None:
                reporter.say(f"  跳过启动测试：{detail}")
            else:
                problems += 1
                reporter.warn(f"  问题：浏览器启动测试失败，{detail}")
    else:
        problems += 1
        reporter.warn(
            "浏览器      : 未找到\n  问题：安装 Chrome/Chromium/Edge，或用 --chrome 指定"
            "（也可设 EPRINTDL_CHROME）"
        )

    import urllib.request

    checks = []
    for name, url in (("站点 HTML", f"{BASE_URL}/"), ("OAI-PMH", f"{BASE_URL}/oai?verb=Identify")):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read(200)
            checks.append({"name": name, "ok": True, "status": response.status})
            reporter.say(f"网络 {name:<9}: HTTP {response.status} 正常")
        except Exception as exc:
            problems += 1
            checks.append({"name": name, "ok": False, "error": str(exc)})
            reporter.warn(f"网络 {name:<9}: 失败 {type(exc).__name__}: {exc}")
    report["checks"] = checks
    report["config"] = config.as_dict()
    report["problems"] = problems
    report["ok"] = problems == 0

    reporter.say(
        f"\n配置文件    : {config.config_file or '（未找到，使用默认值）'}"
        f"\n数据目录    : {config.base_dir}"
        f"\n自检结论：{'全部通过' if problems == 0 else f'有 {problems} 项需要处理'}"
    )
    reporter.finish("doctor", **report)
    return EXIT_OK if problems == 0 else EXIT_PARTIAL_FAILURE


def _browser_launch_check(config: Config, browser: str, needs_windows_path: bool):
    from .cdp import Chrome

    try:
        with ChromeLock(config.lock, timeout=10, port=config.port or None) as lock:
            chrome = Chrome(
                browser,
                config.profile,
                port=lock.port,
                headless=config.headless,
                needs_windows_path=needs_windows_path,
                startup_timeout=25.0,
            )
            try:
                chrome.start()
                version = str(chrome.version.get("Browser") or "版本未知")
            finally:
                chrome.stop()
        return True, version
    except LockTimeout as exc:
        return None, f"另一个下载任务正在运行（{exc}）"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def cmd_init(args: argparse.Namespace) -> int:
    reporter = make_reporter(args)
    target = os.path.abspath(args.path or os.path.join(os.getcwd(), "eprintdl.toml"))
    if os.path.exists(target) and not args.force:
        reporter.warn(f"已存在：{target}（如需覆盖请加 --force）")
        reporter.finish("init", ok=False, path=target, created=False)
        return EXIT_USAGE
    write_default_config(target)
    reporter.say(f"已写出配置模板：{target}")
    reporter.finish("init", path=target, created=True)
    return EXIT_OK


def cmd_harvest(args: argparse.Namespace) -> int:
    reporter = make_reporter(args)
    config = build_config(args)
    store = Store(config.db)
    limiter = RateLimiter(config.rate)
    client = OaiClient(limiter=limiter)

    years = parse_years(args.years)
    year_from = args.year_from if args.year_from is not None else (years[0] if years else None)
    year_to = args.year_to if args.year_to is not None else (years[-1] if years else None)
    if year_from is None and year_to is None and not args.all:
        reporter.warn("请用 --from/--to/--years 指定年份范围；全量请显式加 --all。")
        store.close()
        return EXIT_USAGE
    year_from = 1996 if year_from is None else year_from
    year_to = time.localtime().tm_year if year_to is None else year_to
    if year_from > year_to:
        year_from, year_to = year_to, year_from

    reporter.say(f"采集元数据：{year_from} - {year_to}（{BASE_URL}/oai，限速 {config.rate}s）")
    oai_from = f"{max(1996, year_from - 1)}-12-20"
    oai_until = f"{min(2100, year_to + 1)}-01-10"
    resume_token = store.get_meta("harvest.token")
    if resume_token:
        reporter.say("  从上次中断处续采")
    kept = 0
    try:
        for batch in client.harvest(
            date_from=oai_from,
            date_until=oai_until,
            resumption_token=resume_token,
            on_batch=lambda _records, tok: store.set_meta("harvest.token", tok),
            progress=lambda n, _tok: reporter.say(f"  OAI 已采集 {n} 条"),
        ):
            wanted = [
                record for record in batch if year_from <= int(record["paper_id"][:4]) <= year_to
            ]
            store.upsert_metadata(wanted)
            kept += len(wanted)
            reporter.event("oai_batch", kept=kept, batch=len(batch))
    except OaiError as exc:
        reporter.warn(f"OAI 采集失败: {exc}")
        store.log_event("harvest_error", None, str(exc))
    store.set_meta("harvest.token", None)
    reporter.say(f"OAI 阶段完成：保留 {kept} 条（编号年份在范围内）")

    browse_counts: dict[str, int] = {}
    if args.fill_gaps:
        reporter.say("对齐年度列表页（权威归属年）并补齐遗漏...")
        for year in range(year_from, year_to + 1):
            try:
                records = harvest_year(
                    year, limiter=limiter, progress=lambda m: reporter.say(m)
                )
            except FetchError as exc:
                if exc.status == 403:
                    reporter.warn(
                        f"  {year} 年度页被站点拒绝（HTTP 403）：当前网络下站点对普通页面请求也启用了验证，"
                        "请稍后重试或更换网络；OAI-PMH 阶段的数据不受影响。"
                    )
                else:
                    reporter.warn(f"  {year} 年度页读取失败: {exc}")
                continue
            if not records:
                reporter.say(f"  {year}: 年度页无内容")
                continue
            known = {row["paper_id"] for row in store.query(years=[year], limit=None)}
            missing = [record for record in records if record["paper_id"] not in known]
            store.upsert_metadata(records)
            store.set_meta(f"browse.count.{year}", len(records))
            browse_counts[str(year)] = len(records)
            reporter.event("year_aligned", year=year, listed=len(records), filled=len(missing))
            reporter.say(f"  {year}: 年度页 {len(records)} 条，补入 {len(missing)} 条")

    snapshot = status_snapshot(store, config)
    store.close()
    reporter.say(f"完成：库中共 {snapshot['totals']['records']} 条记录。")
    reporter.finish(
        "harvest",
        years={"from": year_from, "to": year_to},
        oai_kept=kept,
        browse=browse_counts,
        totals=snapshot["totals"],
    )
    return EXIT_OK


def cmd_enrich(args: argparse.Namespace) -> int:
    reporter = make_reporter(args)
    config = build_config(args)
    store = Store(config.db)
    limiter = RateLimiter(config.rate)
    ids = collect_ids(args)
    if ids:
        register_ids(store, ids)
        rows = store.query(ids=ids, limit=None)
    else:
        rows = store.query(
            years=parse_years(args.years) or None,
            year_from=args.year_from,
            year_to=args.year_to,
            limit=args.limit,
            order=args.order,
        )
    targets = [row for row in rows if args.force or not row["abstract"] or not row["authors"]]
    updated = 0
    for index, row in enumerate(targets, 1):
        parsed = enrich_paper(row["paper_id"], limiter=limiter)
        if parsed:
            store.upsert_metadata([parsed])
            updated += 1
            reporter.event("enriched", paper_id=row["paper_id"])
        if index % 20 == 0 or index == len(targets):
            reporter.say(f"  补齐 {index}/{len(targets)}，已更新 {updated}")
    store.close()
    reporter.say(f"完成：更新 {updated} 条。")
    reporter.finish("enrich", targets=len(targets), updated=updated)
    return EXIT_OK


def _resolve_download_targets(args: argparse.Namespace, store: Store):
    ids = collect_ids(args)
    if ids:
        register_ids(store, ids)
        return ids, store.query(ids=ids, status=args.status, order=args.order)
    rows = store.query(
        years=parse_years(args.years) or None,
        year_from=args.year_from,
        year_to=args.year_to,
        categories=args.category,
        keyword=args.keyword,
        status=args.status,
        limit=args.limit,
        order=args.order,
    )
    return [row["paper_id"] for row in rows], rows


def cmd_download(args: argparse.Namespace) -> int:
    reporter = make_reporter(args)
    config = build_config(args)
    store = Store(config.db)
    limiter = RateLimiter(config.rate)

    ids, rows = _resolve_download_targets(args, store)
    if not rows:
        store.close()
        reporter.say("没有符合条件的论文（可先 harvest，或用 --ids 指定编号）。")
        reporter.finish("download", total=0, downloaded=0, reused=0, failed=0, results=[])
        return EXIT_OK

    if args.dry_run:
        planned = []
        for row in rows:
            stem = paper_stem(row["paper_id"], row["title"] or "", max_len=config.title_len)
            planned.append(
                {
                    "paper_id": row["paper_id"],
                    "file_name": stem + ".pdf",
                    "target": os.path.abspath(os.path.join(config.out, stem + ".pdf")),
                }
            )
        for item in planned[:20]:
            reporter.say(f"  {item['paper_id']}  {item['file_name']}")
        if len(planned) > 20:
            reporter.say(f"  ...（其余 {len(planned) - 20} 篇略）")
        store.close()
        reporter.finish("download", dry_run=True, total=len(planned), planned=planned, results=[])
        return EXIT_OK

    outcome = download_papers(
        config,
        store,
        ids,
        reporter=reporter,
        limiter=limiter,
        retries=config.retries,
        title_len=config.title_len,
        need_title=not args.no_title_fetch,
        lock_timeout=args.lock_timeout,
        use_lock=not args.no_lock,
        port=args.port,
    )
    store.close()
    reporter.say(
        f"下载结束：新下载 {outcome.downloaded} 篇，已存在跳过 {outcome.reused} 篇，"
        f"失败 {outcome.failed} 篇，用时 {outcome.elapsed / 60:.1f} 分钟。"
    )
    if outcome.failed:
        reporter.say("重试失败项：eprintdl retry")
    payload = outcome.as_dict()
    payload["out"] = os.path.abspath(config.out)
    payload["ok"] = outcome.exit_code == EXIT_OK
    reporter.finish("download", **payload)
    return outcome.exit_code


def cmd_retry(args: argparse.Namespace) -> int:
    reporter = make_reporter(args)
    config = build_config(args)
    store = Store(config.db)
    outcome = retry_failed(
        config,
        store,
        reporter=reporter,
        limiter=RateLimiter(config.rate),
        lock_timeout=args.lock_timeout,
        need_title=not args.no_title_fetch,
        port=args.port,
        use_lock=not args.no_lock,
    )
    store.close()
    reporter.say(
        f"重试结束：成功 {outcome.downloaded + outcome.reused} 篇，失败 {outcome.failed} 篇。"
    )
    payload = outcome.as_dict()
    payload["ok"] = outcome.exit_code == EXIT_OK
    reporter.finish("retry", **payload)
    return outcome.exit_code


def cmd_search(args: argparse.Namespace) -> int:
    reporter = make_reporter(args)
    config = build_config(args)
    store = Store(config.db)
    results = search_papers(
        store,
        keyword=args.keyword,
        years=parse_years(args.years) or None,
        year_from=args.year_from,
        year_to=args.year_to,
        categories=args.category,
        status=args.status,
        downloaded_only=args.downloaded_only,
        limit=args.limit or 50,
        order=args.order,
    )
    store.close()
    for item in results:
        mark = os.path.basename(item["file_path"]) if item["file_exists"] else ""
        reporter.say(f"{item['paper_id']:<11} {(item['title'] or '(无标题)')[:60]:<60} {mark}")
    reporter.say(f"命中 {len(results)} 条（数据目录 {config.base_dir}）")
    reporter.finish("search", count=len(results), results=results)
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    reporter = make_reporter(args)
    config = build_config(args)
    store = Store(config.db)
    rows = store.query(
        years=parse_years(args.years) or None,
        year_from=args.year_from,
        year_to=args.year_to,
        categories=args.category,
        keyword=args.keyword,
        status=args.status,
        limit=args.limit or 50,
        offset=args.offset,
        order=args.order,
    )
    marks = {"done": "done", "failed": "fail", "pending": "", "skipped": "skip"}
    payload = []
    for row in rows:
        authors = json.loads(row["authors"] or "[]")
        payload.append(
            {
                "paper_id": row["paper_id"],
                "title": row["title"],
                "authors": authors,
                "status": row["status"],
                "file_path": os.path.abspath(row["file_path"]) if row["file_path"] else None,
            }
        )
        author_text = ", ".join(authors[:3]) + (" 等" if len(authors) > 3 else "")
        reporter.say(
            f"{marks.get(row['status'], '?'):<4} {row['paper_id']:<11} "
            f"{(row['title'] or '(无标题)')[:58]:<58} {author_text}"
        )
    counts = store.counts()
    store.close()
    reporter.say(
        f"共 {counts.get('total', 0)} 条；完成 {counts.get('done', 0)}，"
        f"失败 {counts.get('failed', 0)}，待下载 {counts.get('pending', 0)}"
    )
    reporter.finish("list", count=len(payload), results=payload, totals=counts)
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    reporter = make_reporter(args)
    config = build_config(args)
    store = Store(config.db)
    snapshot = status_snapshot(store, config)
    store.close()
    totals = snapshot["totals"]
    reporter.say(
        f"数据目录：{snapshot['out']}\n"
        f"数据库  ：{snapshot['db']}\n"
        f"记录 {totals['records']} 条（{totals['years']} 个年份）；"
        f"已下载 {totals['downloaded']}，失败 {totals['failed']}，待下载 {totals['pending']}\n"
        f"磁盘上 PDF：{totals['pdf_files_on_disk']} 个 / {human_bytes(totals['pdf_bytes_on_disk'])}"
    )
    for item in snapshot["years"]:
        reporter.say(
            f"  {item['year']}  记录 {item['records']:>5}  已下载 {item['downloaded']:>5}  "
            f"失败 {item['failed']:>4}  最大编号 {item['latest_number']:03d}"
        )
    reporter.finish("status", **snapshot)
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    reporter = make_reporter(args)
    config = build_config(args)
    store = Store(config.db)
    outcome = verify_files(
        store, years=parse_years(args.years) or None, limit=args.limit, reporter=reporter
    )
    store.close()
    reporter.say(f"校验完成：检查 {outcome.checked} 个，损坏或缺失 {len(outcome.broken)} 个。")
    payload = outcome.as_dict()
    payload["ok"] = outcome.ok
    reporter.finish("verify", **payload)
    if outcome.broken and args.redownload:
        reporter.say("开始重下损坏项...")
        return cmd_retry(args)
    return outcome.exit_code


def cmd_export(args: argparse.Namespace) -> int:
    reporter = make_reporter(args)
    config = build_config(args)
    store = Store(config.db)
    rows = store.query(
        years=parse_years(args.years) or None,
        year_from=args.year_from,
        year_to=args.year_to,
        categories=args.category,
        keyword=args.keyword,
        status=args.status,
        limit=None,
        order=args.order,
    )
    fields = [
        "paper_id", "id_year", "title", "authors", "categories", "published", "updated",
        "url", "pdf_url", "status", "file_path", "file_size", "downloaded", "abstract",
    ]
    target = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if target.lower().endswith(".json"):
        payload = []
        for row in rows:
            item = {field: row[field] for field in fields}
            item["authors"] = json.loads(row["authors"] or "[]")
            item["categories"] = json.loads(row["categories"] or "[]")
            payload.append(item)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
    else:
        import csv

        with open(target, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(fields)
            for row in rows:
                record = []
                for field in fields:
                    value = row[field]
                    if field in ("authors", "categories"):
                        value = "; ".join(json.loads(value or "[]"))
                    elif field == "abstract" and value:
                        value = value.replace("\r\n", "\n")
                    record.append(value)
                writer.writerow(record)
    store.close()
    reporter.say(f"已导出 {len(rows)} 条 -> {target}")
    reporter.finish("export", count=len(rows), out=target)
    return EXIT_OK


def cmd_ids(args: argparse.Namespace) -> int:
    reporter = make_reporter(args)
    config = build_config(args)
    store = Store(config.db)
    years = parse_years(args.years) or store.years_present()
    payload = []
    for year in years:
        rows = store.query(years=[year], limit=None)
        item = {
            "year": year,
            "records": len(rows),
            "downloaded": sum(1 for row in rows if row["status"] == "done"),
            "latest_number": max((row["number"] for row in rows), default=0),
        }
        payload.append(item)
        reporter.say(
            f"{year}: 最大编号 {item['latest_number']:03d}，记录 {item['records']}，"
            f"已下载 {item['downloaded']}"
        )
    store.close()
    reporter.finish("ids", years=payload)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eprintdl",
        description="IACR ePrint 论文批量下载（文件名 = <年份>-<编号>_<无空格标题>.pdf）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "给 LLM CLI / 脚本调用：\n"
            "  eprintdl --json search --keyword lattice --limit 10\n"
            "  eprintdl --json download --ids 2024/1000 2023/1234\n"
            "  eprintdl --format json status\n"
            "\n"
            "退出码：0 成功 / 1 有失败项 / 2 参数或配置错 / 3 等待下载锁超时\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"eprintdl {__version__}")
    parser.add_argument("--config", help="指定 eprintdl.toml（默认向上查找）")
    parser.add_argument("--json", action="store_true", help="等价于 --format ndjson")
    parser.add_argument("--format", choices=["human", "json", "ndjson"], help="输出格式")
    parser.add_argument("--db", help="覆盖数据库路径")
    parser.add_argument("--quiet", action="store_true", help="减少过程输出")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_filters(target: argparse.ArgumentParser) -> None:
        target.add_argument("--years", nargs="*", help="归档年份，如 2024 或 2020-2024")
        target.add_argument("--from", dest="year_from", type=int, help="起始年份（含）")
        target.add_argument("--to", dest="year_to", type=int, help="结束年份（含）")
        target.add_argument("--keyword", help="在标题/作者/摘要/分类中搜索")
        target.add_argument("--category", action="append", help="按分类过滤，可重复")
        target.add_argument("--status", nargs="*", help="pending / done / failed")
        target.add_argument("--limit", type=int, help="最多处理多少条")
        target.add_argument("--order", choices=["oldest", "newest"], default="oldest")

    def add_download_knobs(target: argparse.ArgumentParser) -> None:
        target.add_argument("--out", help="PDF 输出目录（覆盖配置）")
        target.add_argument("--rate", type=float, help="每篇最小间隔秒数（覆盖配置）")
        target.add_argument("--retries", type=int, help="单篇最大尝试次数")
        target.add_argument("--timeout", type=float, help="单篇等待秒数")
        target.add_argument("--title-len", type=int, help="文件名中标题最大长度")
        target.add_argument(
            "--chrome", help="浏览器可执行文件路径（WSL 里可指向 Windows 的 chrome.exe）"
        )
        target.add_argument("--profile", help="浏览器配置目录")
        target.add_argument(
            "--port", type=int, default=None, help="调试端口（默认自动分配）"
        )
        target.add_argument("--headless", action="store_true", help="无头模式（更易被拦）")
        target.add_argument(
            "--lock-timeout", type=float, default=120.0, help="等待其它下载任务的最长秒数"
        )
        target.add_argument("--no-lock", action="store_true", help="跳过文件锁（自行保证串行）")
        target.add_argument("--no-title-fetch", action="store_true", help="缺标题时不抓单篇页面")

    p_doctor = sub.add_parser("doctor", help="环境自检（Python/平台/浏览器/网络/配置）")
    p_doctor.add_argument("--chrome")
    p_doctor.set_defaults(func=cmd_doctor)

    p_init = sub.add_parser("init", help="写出 eprintdl.toml 配置模板")
    p_init.add_argument("--path", help="写出位置（默认当前目录）")
    p_init.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    p_init.set_defaults(func=cmd_init)

    p_harvest = sub.add_parser("harvest", help="采集元数据（OAI-PMH + 年度页对齐）")
    p_harvest.add_argument("--years", nargs="*")
    p_harvest.add_argument("--from", dest="year_from", type=int)
    p_harvest.add_argument("--to", dest="year_to", type=int)
    p_harvest.add_argument("--all", action="store_true", help="不给范围时全量 1996 至今")
    p_harvest.add_argument("--rate", type=float)
    p_harvest.add_argument("--no-fill-gaps", dest="fill_gaps", action="store_false")
    p_harvest.set_defaults(func=cmd_harvest, fill_gaps=True)

    p_enrich = sub.add_parser("enrich", help="抓单篇页面补齐作者/摘要/分类")
    add_filters(p_enrich)
    p_enrich.add_argument("--ids", nargs="*")
    p_enrich.add_argument("--id-file")
    p_enrich.add_argument("--rate", type=float)
    p_enrich.add_argument("--force", action="store_true")
    p_enrich.set_defaults(func=cmd_enrich)

    p_dl = sub.add_parser("download", help="批量下载 PDF")
    add_filters(p_dl)
    add_download_knobs(p_dl)
    p_dl.add_argument("--ids", nargs="*", help="直接指定编号，如 2024/1000 2023/1234")
    p_dl.add_argument("--id-file", help="每行一个编号的文件")
    p_dl.add_argument("--dry-run", action="store_true", help="只输出将生成的文件名")
    p_dl.set_defaults(func=cmd_download, status=["pending", "failed"])

    p_retry = sub.add_parser("retry", help="重跑所有 failed 条目")
    add_download_knobs(p_retry)
    p_retry.set_defaults(func=cmd_retry)

    p_search = sub.add_parser("search", help="检索库中论文（JSON 友好）")
    add_filters(p_search)
    p_search.add_argument("--downloaded-only", action="store_true", help="只返回已下载且文件存在")
    p_search.set_defaults(func=cmd_search, order="newest")

    p_list = sub.add_parser("list", help="列出库中论文（人类可读）")
    add_filters(p_list)
    p_list.add_argument("--offset", type=int, default=0)
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser("status", help="库与配置状态")
    p_status.set_defaults(func=cmd_status)

    p_verify = sub.add_parser("verify", help="校验已下载文件完整性")
    p_verify.add_argument("--years", nargs="*")
    p_verify.add_argument("--limit", type=int)
    p_verify.add_argument("--redownload", action="store_true", help="校验后重下损坏项")
    add_download_knobs(p_verify)
    p_verify.set_defaults(func=cmd_verify)

    p_export = sub.add_parser("export", help="导出元数据为 CSV/JSON")
    add_filters(p_export)
    p_export.add_argument("--out", default=os.path.join(os.getcwd(), "papers.csv"))
    p_export.set_defaults(func=cmd_export)

    p_ids = sub.add_parser("ids", help="查看各年最大编号")
    p_ids.add_argument("--years", nargs="*")
    p_ids.set_defaults(func=cmd_ids)

    return parser


def _normalize_global_options(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    takes_value = {"--db", "--config", "--format"}
    moved: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in takes_value and index + 1 < len(argv):
            moved += [token, argv[index + 1]]
            index += 2
            continue
        if any(token.startswith(flag + "=") for flag in takes_value):
            moved.append(token)
            index += 1
            continue
        rest.append(token)
        index += 1
    return moved + rest


def _ensure_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            if (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: Iterable[str] | None = None) -> int:
    _ensure_utf8_output()
    parser = build_parser()
    raw = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(_normalize_global_options(raw))
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        Reporter().warn("已中断。重跑同一命令即可续跑（已下载文件会自动跳过）。")
        return 130
    except SystemExit as exc:
        Reporter().warn(str(exc))
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
