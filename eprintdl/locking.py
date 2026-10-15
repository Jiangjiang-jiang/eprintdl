from __future__ import annotations

import errno
import json
import os
import socket
import threading
import time
from typing import IO

DEFAULT_PORT_RANGE = (9222, 9321)
HEARTBEAT_SECONDS = 5.0
STALE_SECONDS = 60.0


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import subprocess

        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return str(pid) in (result.stdout or "")
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def port_available(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) != 0


def pick_free_port(start: int = DEFAULT_PORT_RANGE[0], end: int = DEFAULT_PORT_RANGE[1]) -> int:
    for port in range(max(1, start), max(2, end) + 1):
        if port_available(port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LockTimeout(RuntimeError):
    pass


class ChromeLock:

    def __init__(
        self,
        path: str,
        *,
        timeout: float = 120.0,
        port: int | None = None,
        port_range: tuple[int, int] = DEFAULT_PORT_RANGE,
        message: "callable | None" = None,
    ) -> None:
        self.path = os.path.abspath(path)
        self.timeout = timeout
        self.requested_port = port or 0
        self.port_range = port_range
        self._say = message
        self.port: int | None = None
        self._held = False
        self._heartbeat: threading.Thread | None = None
        self._stop = threading.Event()

    def _notify(self, text: str) -> None:
        if self._say:
            self._say(text)

    def _read_lock(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return {}

    def _write_lock(self) -> None:
        payload = {"pid": os.getpid(), "port": self.port, "started": time.time()}
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def _try_acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            info = self._read_lock()
            holder = int(info.get("pid") or 0)
            age = time.time() - os.path.getmtime(self.path)
            if holder and not pid_alive(holder):
                self._notify(f"  发现失效锁（pid {holder} 已退出），清理后重试")
                self._remove_lock()
                return False
            if age > STALE_SECONDS and not holder:
                self._notify("  锁文件已过期且无持有者信息，清理后重试")
                self._remove_lock()
                return False
            return False
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "port": None, "started": time.time()}, stream)
        return True

    def _remove_lock(self) -> None:
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                os.utime(self.path, None)
            except OSError:
                return

    def __enter__(self) -> "ChromeLock":
        deadline = time.monotonic() + max(0.0, self.timeout)
        waited = False
        while True:
            if self._try_acquire():
                break
            if time.monotonic() >= deadline:
                info = self._read_lock()
                raise LockTimeout(
                    f"等待下载锁超时（{self.timeout:.0f}s）。"
                    f"锁文件 {self.path} 由 pid {info.get('pid')} 持有；"
                    f"可等待其结束后重试，或删除该文件（确认无其它下载在跑）。"
                )
            if not waited:
                self._notify(f"  另一个下载任务正在运行，等待锁（最多 {self.timeout:.0f}s）...")
                waited = True
            time.sleep(0.5)
        self._held = True
        self.port = self.requested_port or pick_free_port(*self.port_range)
        self._write_lock()
        self._heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._held:
            self._remove_lock()
            self._held = False
