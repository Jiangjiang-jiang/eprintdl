from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


class Client:
    def __init__(self, command: list[str], env: dict) -> None:
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
        self.next_id = 0
        self.stderr_lines: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_lines.append(line.rstrip())

    def call(self, method: str, params: dict | None = None, timeout: float = 120.0) -> dict:
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("服务端关闭了 stdout")
            payload = json.loads(line)
            if payload.get("id") == self.next_id:
                return payload
        raise TimeoutError(method)

    def notify(self, method: str, params: dict | None = None) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n"
        )
        self.proc.stdin.flush()

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except Exception:
            self.proc.kill()


def main() -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["PYTHONIOENCODING"] = "utf-8"
    command = [PY, "-X", "utf8", "-m", "eprintdl.mcp_server"]
    print("命令:", " ".join(command))
    client = Client(command, env)
    failures = 0

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  {'通过' if condition else '失败'} {label}" + (f"  {detail}" if detail else ""))
        if not condition:
            failures += 1

    try:
        print("[1] initialize")
        reply = client.call("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "self-test", "version": "1.0"},
        })
        result = reply.get("result", {})
        check("有 result", "result" in reply)
        check("协议版本", bool(result.get("protocolVersion")), result.get("protocolVersion", ""))
        check("serverInfo.name", result.get("serverInfo", {}).get("name") == "eprintdl")
        check("声明 tools 能力", "tools" in result.get("capabilities", {}))
        client.notify("notifications/initialized")

        print("[2] tools/list")
        reply = client.call("tools/list")
        tools = reply.get("result", {}).get("tools", [])
        names = [t["name"] for t in tools]
        check("返回 4 个工具", len(tools) == 4, str(names))
        check(
            "工具齐全",
            set(names) == {"eprint_search", "eprint_download", "eprint_status", "eprint_verify"},
        )
        for tool in tools:
            check(f"{tool['name']} 有 inputSchema", isinstance(tool.get("inputSchema"), dict))
            check(f"{tool['name']} 有 description", bool(tool.get("description")))

        print("[3] tools/call eprint_status")
        reply = client.call("tools/call", {"name": "eprint_status", "arguments": {}})
        structured = reply.get("result", {}).get("structuredContent", {})
        check("没有协议错误", "error" not in reply, str(reply.get("error", ""))[:80])
        check("返回 totals", "totals" in structured)
        check(
            "totals 含 records",
            isinstance(structured.get("totals", {}).get("records"), int),
            f"records={structured.get('totals', {}).get('records')}",
        )
        check("content 是文本块", reply["result"]["content"][0]["type"] == "text")

        print("[4] tools/call eprint_search")
        reply = client.call(
            "tools/call",
            {"name": "eprint_search", "arguments": {"keyword": "lattice", "limit": 2}},
        )
        payload = reply.get("result", {}).get("structuredContent", {})
        check("命中数量正确", payload.get("count") == 2, f"count={payload.get('count')}")
        first = (payload.get("results") or [{}])[0]
        check("结果含 paper_id", bool(first.get("paper_id")), first.get("paper_id", ""))
        check("结果含 file_path 字段", "file_path" in first)

        print("[5] 未知工具 / 错误处理")
        reply = client.call("tools/call", {"name": "nope", "arguments": {}})
        check("未知工具报 INVALID_PARAMS", reply.get("error", {}).get("code") == -32602)

        print("[6] 未知方法")
        reply = client.call("bogus/method")
        check("未知方法报 METHOD_NOT_FOUND", reply.get("error", {}).get("code") == -32601)

        print("[7] 坏参数")
        reply = client.call("tools/call", {"name": "eprint_download", "arguments": {}})
        check("空 paper_ids 报错", reply.get("error", {}).get("code") == -32602,
              str(reply.get("error", {}).get("message", ""))[:60])

        if "--with-download" in sys.argv:
            print("[8] tools/call eprint_download（真实下载，会启动 Chrome）")
            started = time.time()
            reply = client.call(
                "tools/call",
                {
                    "name": "eprint_download",
                    "arguments": {"paper_ids": ["2024/078"], "lock_timeout": 60},
                },
                timeout=600,
            )
            payload = reply.get("result", {}).get("structuredContent", {})
            check("无协议错误", "error" not in reply, str(reply.get("error", ""))[:80])
            check("isError 为 false", reply.get("result", {}).get("isError") is False)
            check("downloaded+reused == 1",
                  payload.get("downloaded", 0) + payload.get("reused", 0) == 1,
                  f"downloaded={payload.get('downloaded')} reused={payload.get('reused')}")
            result = (payload.get("results") or [{}])[0]
            path = result.get("file_path") or ""
            check("返回绝对路径", os.path.isabs(path), path)
            check("文件确实存在", bool(path) and os.path.exists(path),
                  f"{os.path.getsize(path)} 字节" if os.path.exists(path) else "不存在")
            if os.path.exists(path):
                with open(path, "rb") as handle:
                    check("是 PDF", handle.read(5) == b"%PDF-")
            print(f"     用时 {time.time() - started:.1f}s，状态 {result.get('status')}")
    finally:
        client.close()

    print("\n服务端 stderr 末尾:")
    for line in client.stderr_lines[-4:]:
        print("   ", line)
    print(f"\n自测结论：{'全部通过' if failures == 0 else f'{failures} 项失败'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
