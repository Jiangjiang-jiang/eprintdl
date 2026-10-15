from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Callable

from . import __version__
from .config import load_config
from .reporter import Reporter
from .service import (
    EXIT_OK,
    download_papers,
    normalize_ids,
    search_papers,
    status_snapshot,
    verify_files,
)
from .store import Store
from .utils import RateLimiter

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "eprintdl"
SERVER_TITLE = "IACR ePrint downloader"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _log(message: str) -> None:
    sys.stderr.write(f"[eprintdl-mcp] {message}\n")
    sys.stderr.flush()

_DEVNULL = open(os.devnull, "w")


def _quiet_reporter() -> Reporter:
    return Reporter("ndjson", stream=_DEVNULL, err_stream=sys.stderr)


def tool_search(args: dict) -> dict:
    config = load_config(args.get("config"))
    store = Store(config.db)
    try:
        results = search_papers(
            store,
            keyword=args.get("keyword"),
            years=_years(args),
            year_from=args.get("year_from") or args.get("from"),
            year_to=args.get("year_to") or args.get("to"),
            categories=[args["category"]] if args.get("category") else None,
            downloaded_only=bool(args.get("downloaded_only")),
            limit=int(args.get("limit") or 20),
            order=args.get("order") or "newest",
        )
    finally:
        store.close()
    return {
        "count": len(results),
        "results": results,
        "hint": "用 eprint_download 下载 results[].paper_id；已下载的条目 file_path 即本地绝对路径。",
    }


def _years(args: dict) -> list[int] | None:
    raw = args.get("years")
    if not raw:
        return None
    years: list[int] = []
    for item in raw if isinstance(raw, list) else [raw]:
        text = str(item)
        if "-" in text:
            start, _, end = text.partition("-")
            if start.strip().isdigit() and end.strip().isdigit():
                years += list(range(int(start), int(end) + 1))
                continue
        if text.strip().isdigit():
            years.append(int(text.strip()))
    return sorted(set(years)) or None


def tool_download(args: dict) -> dict:
    config = load_config(args.get("config"))
    raw_ids = args.get("paper_ids") or args.get("ids") or []
    if isinstance(raw_ids, str):
        raw_ids = raw_ids.replace(",", " ").split()
    ids = normalize_ids(raw_ids)
    if not ids:
        raise ValueError("paper_ids 不能为空，例如 [\"2024/1000\"]")

    store = Store(config.db)
    reporter = _quiet_reporter()
    try:
        outcome = download_papers(
            config,
            store,
            ids,
            reporter=reporter,
            limiter=RateLimiter(config.rate),
            retries=config.retries,
            title_len=config.title_len,
            need_title=not args.get("no_title_fetch", False),
            lock_timeout=float(args.get("lock_timeout") or 300.0),
            use_lock=not args.get("no_lock", False),
            port=args.get("port"),
        )
    finally:
        store.close()
    payload = outcome.as_dict()
    payload["ok"] = outcome.exit_code == EXIT_OK
    payload["out"] = os.path.abspath(config.out)
    if outcome.results:
        payload["hint"] = "results[].file_path 是可直接读取的绝对路径。"
    return payload


def tool_status(args: dict) -> dict:
    config = load_config(args.get("config"))
    store = Store(config.db)
    try:
        return status_snapshot(store, config)
    finally:
        store.close()


def tool_verify(args: dict) -> dict:
    config = load_config(args.get("config"))
    store = Store(config.db)
    try:
        outcome = verify_files(store, years=_years(args), limit=args.get("limit"))
    finally:
        store.close()
    payload = outcome.as_dict()
    payload["ok"] = outcome.ok
    return payload

TOOLS: list[dict] = [
    {
        "name": "eprint_search",
        "title": "Search local eprint library",
        "description": (
            "在本地 eprint 元数据库里检索论文（标题/作者/摘要/分类）。下载前先用它确认编号，"
            "已下载条目的 file_path 是可直接读取的本地绝对路径。库里没有该年份时先调用 harvest。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "关键词，匹配标题/作者/摘要/分类"},
                "years": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": '归档年份，如 ["2024"] 或 ["2020-2024"]',
                },
                "from": {"type": "integer", "description": "起始年份（含）"},
                "to": {"type": "integer", "description": "结束年份（含）"},
                "category": {"type": "string", "description": '分类，如 "Foundations"'},
                "downloaded_only": {"type": "boolean", "description": "只返回已下载且文件存在的条目"},
                "limit": {"type": "integer", "description": "最多返回多少条（默认 20）"},
                "order": {"type": "string", "enum": ["oldest", "newest"], "description": "排序"},
            },
            "additionalProperties": False,
        },
        "handler": tool_search,
    },
    {
        "name": "eprint_download",
        "title": "Download eprint papers",
        "description": (
            "按归档编号下载 eprint 论文的原始 PDF 到本地，返回绝对路径。"
            "单篇约 0.5 到 2 秒；首次或凭据失效时需过一次 Cloudflare 质询（约 2 到 8 秒）。"
            "会启动一个真实 Chrome 窗口（PDF 路径用 HTTP 客户端会 403，这是预期行为）。"
            "已存在的文件会被复用（status=reused），因此可安全重复调用。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "paper_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": '归档编号列表，如 ["2024/1000", "2023/1234"]',
                },
                "no_title_fetch": {
                    "type": "boolean",
                    "description": "缺元数据时不抓单篇页面（文件名只剩编号）",
                },
                "lock_timeout": {
                    "type": "number",
                    "description": "等待其它下载任务的最长秒数（默认 300）",
                },
            },
            "required": ["paper_ids"],
            "additionalProperties": False,
        },
        "handler": tool_download,
    },
    {
        "name": "eprint_status",
        "title": "Library status",
        "description": "返回本地库与配置状态：记录数、已下载/失败/待下载、各年份进度、磁盘上 PDF 数量与体积。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_status,
    },
    {
        "name": "eprint_verify",
        "title": "Verify downloaded PDFs",
        "description": "校验已下载文件是否为完整 PDF（文件头、%%EOF、体积），列出损坏项。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "years": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        "handler": tool_verify,
    },
]


def _tool_index() -> dict[str, dict]:
    return {tool["name"]: tool for tool in TOOLS}


class Server:
    def __init__(self, stdin=None, stdout=None) -> None:
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.initialized = False

    def _send(self, message: dict) -> None:
        line = json.dumps(message, ensure_ascii=False)
        self.stdout.write(line + "\n")
        self.stdout.flush()

    def _reply(self, request_id: Any, result: dict) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(self, request_id: Any, code: int, message: str, data: Any = None) -> None:
        error: dict = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._send({"jsonrpc": "2.0", "id": request_id, "error": error})

    def handle_message(self, message: dict) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method is None and "result" in message:
            return
        if request_id is None:
            if method == "notifications/initialized":
                self.initialized = True
            return

        try:
            if method == "initialize":
                protocol = params.get("protocolVersion") or PROTOCOL_VERSION
                self._reply(
                    request_id,
                    {
                        "protocolVersion": protocol,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {
                            "name": SERVER_NAME,
                            "title": SERVER_TITLE,
                            "version": __version__,
                        },
                        "instructions": (
                            "eprintdl：下载 IACR ePrint 论文。检索用 eprint_search，"
                            "下载用 eprint_download（返回本地绝对路径）。下载会启动 Chrome 窗口，属正常现象。"
                        ),
                    },
                )
            elif method == "ping":
                self._reply(request_id, {})
            elif method == "tools/list":
                self._reply(
                    request_id,
                    {
                        "tools": [
                            {
                                "name": tool["name"],
                                "title": tool["title"],
                                "description": tool["description"],
                                "inputSchema": tool["inputSchema"],
                            }
                            for tool in TOOLS
                        ]
                    },
                )
            elif method == "tools/call":
                self._handle_tool_call(request_id, params)
            elif method in ("resources/list", "prompts/list"):
                key = method.split("/")[0]
                self._reply(request_id, {key: []})
            else:
                self._error(request_id, METHOD_NOT_FOUND, f"不支持的方法: {method}")
        except Exception as exc:
            _log(f"处理 {method} 时异常: {exc}\n{traceback.format_exc()}")
            self._error(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    def _handle_tool_call(self, request_id: Any, params: dict) -> None:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        tool = _tool_index().get(name)
        if tool is None:
            self._error(request_id, INVALID_PARAMS, f"未知工具: {name}")
            return
        handler: Callable[[dict], dict] = tool["handler"]
        try:
            payload = handler(arguments)
            failed = isinstance(payload, dict) and payload.get("ok") is False
            self._reply(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(payload, ensure_ascii=False, indent=2),
                        }
                    ],
                    "structuredContent": payload,
                    "isError": bool(failed),
                },
            )
        except ValueError as exc:
            self._error(request_id, INVALID_PARAMS, str(exc))
        except Exception as exc:
            _log(f"工具 {name} 失败: {exc}\n{traceback.format_exc()}")
            self._reply(
                request_id,
                {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                },
            )

    def serve_forever(self) -> int:
        for raw in self.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                self._error(None, PARSE_ERROR, f"JSON 解析失败: {exc}")
                continue
            if isinstance(message, list):
                for item in message:
                    self.handle_message(item)
            else:
                self.handle_message(message)
        _log("stdin 关闭，服务退出")
        return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    _log(f"启动 {SERVER_NAME} {__version__}（协议 {PROTOCOL_VERSION}）")
    return Server().serve_forever()


if __name__ == "__main__":
    sys.exit(main())
