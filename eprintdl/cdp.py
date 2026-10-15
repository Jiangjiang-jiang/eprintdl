from __future__ import annotations

import base64
import json
import os
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from typing import Any, Callable


class CdpError(RuntimeError):
    pass


class WebSocket:

    def __init__(self, url: str, timeout: float = 60.0) -> None:
        m = re.match(r"ws://([^:/]+):(\d+)(/.*)", url)
        if not m:
            raise ValueError(f"不支持的 WebSocket 地址: {url}")
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(None)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\nOrigin: http://{host}:{port}\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CdpError("WebSocket 握手时连接被关闭")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0] + b" ":
            raise CdpError("WebSocket 握手失败: " + head.decode("latin1")[:200])
        self._buf = rest
        self._send_lock = threading.Lock()
        self.closed = False

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise CdpError("WebSocket 连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_text(self, text: str) -> None:
        payload = text.encode()
        mask = os.urandom(4)
        n = len(payload)
        header = bytearray([0x81])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(bytes(header) + masked)

    def recv_text(self) -> str:
        payload = b""
        while True:
            b1, b2 = self._read_exact(2)
            opcode, length = b1 & 0x0F, b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            data = self._read_exact(length) if length else b""
            if opcode == 0x8:
                self.closed = True
                raise CdpError("WebSocket 已被对端关闭")
            if opcode == 0x9:
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                payload = data
                if b1 & 0x80:
                    return payload.decode("utf-8", "replace")
            elif opcode == 0x0:
                payload += data
                if b1 & 0x80:
                    return payload.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


class CdpConnection:

    def __init__(self, ws_url: str, timeout: float = 60.0) -> None:
        self.ws = WebSocket(ws_url, timeout=timeout)
        self._timeout = timeout
        self._next_id = 0
        self._pending: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._event_handlers: list[Callable[[dict], None]] = []
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def on_event(self, handler: Callable[[dict], None]) -> None:
        self._event_handlers.append(handler)

    @staticmethod
    def _safe_call(handler: Callable[[dict], None], message: dict) -> None:
        try:
            handler(message)
        except Exception:
            pass

    def _read_loop(self) -> None:
        while not self._closed:
            try:
                message = json.loads(self.ws.recv_text())
            except Exception:
                break
            if "id" in message:
                with self._lock:
                    slot = self._pending.get(message["id"])
                if slot is not None:
                    slot["result"] = message
                    slot["event"].set()
            elif "method" in message:
                for handler in list(self._event_handlers):
                    threading.Thread(
                        target=self._safe_call, args=(handler, message), daemon=True
                    ).start()
        self._closed = True
        with self._lock:
            for slot in self._pending.values():
                slot["event"].set()

    def call(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            slot = {"event": threading.Event(), "result": None}
            self._pending[mid] = slot
        try:
            self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
            if not slot["event"].wait(timeout or self._timeout):
                raise TimeoutError(f"CDP 命令超时: {method}")
            message = slot["result"]
            if message is None:
                raise CdpError(f"连接中断: {method}")
            if "error" in message:
                raise CdpError(f"{method} 失败: {message['error']}")
            return message.get("result", {})
        finally:
            with self._lock:
                self._pending.pop(mid, None)

    def close(self) -> None:
        self._closed = True
        self.ws.close()


class Chrome:

    def __init__(
        self,
        chrome_path: str,
        user_data_dir: str,
        port: int = 9222,
        headless: bool = False,
        log_path: str | None = None,
        needs_windows_path: bool = False,
        startup_timeout: float = 60.0,
    ) -> None:
        self.startup_timeout = startup_timeout
        self.chrome_path = chrome_path
        self.user_data_dir = user_data_dir
        self.port = port
        self.headless = headless
        self.log_path = log_path or os.path.join(user_data_dir, "chrome.log")
        self.needs_windows_path = needs_windows_path
        self.proc: subprocess.Popen | None = None
        self.conn: CdpConnection | None = None

    def _arg_path(self, path: str) -> str:
        if not self.needs_windows_path:
            return path
        from .platform import to_windows_path

        converted = to_windows_path(path)
        return converted or path

    def start(self, extra_args: list[str] | None = None) -> CdpConnection:
        if not os.path.isfile(self.chrome_path):
            raise FileNotFoundError(f"找不到浏览器: {self.chrome_path}")
        os.makedirs(self.user_data_dir, exist_ok=True)
        data_dir_arg = self._arg_path(self.user_data_dir)
        args = [
            self.chrome_path,
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-crash-reporter",
            "--disable-breakpad",
            "--disable-features=Translate,MediaRouter",
            "--disable-background-networking",
            f"--user-data-dir={data_dir_arg}",
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",
            "about:blank",
        ]
        if self.headless:
            args.insert(1, "--headless=new")
        if extra_args:
            args.extend(extra_args)
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(args, stdout=log, stderr=log)
        deadline = time.time() + self.startup_timeout
        version = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/version", timeout=2
                ) as resp:
                    version = json.load(resp)
                break
            except Exception:
                if self.proc.poll() is not None:
                    raise CdpError(f"浏览器启动后立刻退出，日志见 {self.log_path}")
                time.sleep(0.4)
        if not version:
            self._kill()
            raise TimeoutError(f"等待浏览器调试端口超时（{int(self.startup_timeout)} 秒内 127.0.0.1:{self.port} 无响应）")
        self.version = version
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=10) as resp:
            targets = [t for t in json.load(resp) if t.get("type") == "page"]
        if not targets:
            self._kill()
            raise CdpError("浏览器没有可用的页面目标")
        self.conn = CdpConnection(targets[0]["webSocketDebuggerUrl"], timeout=180)
        self.conn.call("Page.enable")
        return self.conn

    def _kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                pass

    def stop(self) -> None:
        if self.conn:
            self.conn.close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()

    def __enter__(self) -> "Chrome":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
