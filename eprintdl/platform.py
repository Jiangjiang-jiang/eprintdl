from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field


def is_wsl() -> bool:
    if sys.platform != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="replace") as handle:
            version = handle.read().lower()
    except OSError:
        return False
    return "microsoft" in version or "wsl" in version


@dataclass
class Platform:
    os_name: str
    in_wsl: bool
    windows_interop: bool
    notes: list[str] = field(default_factory=list)

    @property
    def is_windows(self) -> bool:
        return self.os_name == "windows"


def detect_platform() -> Platform:
    if os.name == "nt":
        return Platform("windows", in_wsl=False, windows_interop=True)
    wsl = is_wsl()
    interop = wsl and shutil.which("cmd.exe") is not None
    notes: list[str] = []
    if wsl and not interop:
        notes.append("检测到 WSL，但无法执行 .exe（interop 未启用），将只查找 Linux 浏览器。")
    if wsl:
        notes.append(
            "WSL 环境：会优先使用 Linux 浏览器；没有时尝试调用 Windows 的 Chrome。"
        )
    name = "darwin" if sys.platform == "darwin" else ("linux" if sys.platform.startswith("linux") else sys.platform)
    return Platform(name, in_wsl=wsl, windows_interop=interop, notes=notes)


def to_windows_path(path: str) -> str | None:
    if os.name == "nt":
        return path

    text = str(path)
    if shutil.which("wslpath"):
        try:
            result = subprocess.run(
                ["wslpath", "-w", text], capture_output=True, text=True, timeout=15
            )
            converted = (result.stdout or "").strip()
            if result.returncode == 0 and converted:
                return converted
        except Exception:
            pass
    normalized = text if text.startswith("/") else os.path.abspath(text)
    parts = normalized.split("/")
    if len(parts) > 3 and parts[1] == "mnt" and len(parts[2]) == 1:
        drive = parts[2].upper()
        rest = "\\".join(parts[3:])
        return f"{drive}:\\{rest}" if rest else f"{drive}:\\"
    return None


def browser_arg_path(path: str, *, browser_is_windows: bool) -> str:
    if browser_is_windows:
        converted = to_windows_path(path)
        if converted:
            return converted
    return path


def _exists_file(path: str | None) -> bool:
    return bool(path) and os.path.isfile(path)


def _which(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def find_browsers(explicit: str | None = None) -> list[tuple[str, bool]]:
    platform = detect_platform()
    native = platform.is_windows
    candidates: list[tuple[str, bool]] = []

    def add(path: str | None, windows_program: bool) -> None:
        if not path:
            return
        candidates.append((path, bool(windows_program) and not native))

    if explicit:
        add(explicit, explicit.lower().endswith(".exe"))

    env_override = os.environ.get("EPRINTDL_CHROME")
    if env_override:
        add(env_override, env_override.lower().endswith(".exe"))

    if platform.is_windows:
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = os.environ.get(var)
            if not root:
                continue
            add(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"), True)
            add(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"), True)
        add(_which("chrome.exe"), True)
        add(_which("msedge.exe"), True)
    elif platform.os_name == "darwin":
        add("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", False)
        add("/Applications/Chromium.app/Contents/MacOS/Chromium", False)
        add("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", False)
        for name in ("google-chrome", "chromium", "chromium-browser", "msedge"):
            add(_which(name), False)
    else:
        for name in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "microsoft-edge",
            "brave-browser",
        ):
            add(_which(name), False)
        add("/opt/google/chrome/chrome", False)
        add("/usr/bin/chromium", False)

    if platform.in_wsl and platform.windows_interop:
        add("/mnt/c/Program Files/Google/Chrome/Application/chrome.exe", True)
        add("/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe", True)
        add("/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe", True)
        add("/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", True)
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            resolved = _windows_localappdata()
            local = resolved
        if local:
            add(
                os.path.join(
                    local.rstrip("/"), "Google", "Chrome", "Application", "chrome.exe"
                ),
                True,
            )

    seen: set[str] = set()
    usable: list[tuple[str, bool]] = []
    for path, needs_win_paths in candidates:
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        if _exists_file(path):
            usable.append((path, needs_win_paths))
    return usable


def _windows_localappdata() -> str | None:
    if shutil.which("cmd.exe") is None:
        return None
    try:
        result = subprocess.run(
            ["cmd.exe", "/c", "echo", "%LOCALAPPDATA%"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        raw = (result.stdout or "").strip()
    except Exception:
        return None
    if not raw or "%" in raw:
        return None
    normalized = raw.replace("\\", "/")
    if len(normalized) > 1 and normalized[1] == ":":
        drive = normalized[0].lower()
        return f"/mnt/{drive}{normalized[2:]}"
    return None


def resolve_browser(explicit: str | None = None) -> tuple[str | None, bool]:
    if explicit:
        cleaned = explicit.strip().strip('"').strip("'")
        if os.path.isfile(cleaned):
            needs_win_paths = cleaned.lower().endswith(".exe") and os.name != "nt"
            return cleaned, needs_win_paths
    browsers = find_browsers()
    if not browsers:
        return None, False
    return browsers[0]


def describe_platform() -> dict:
    platform = detect_platform()
    browsers = find_browsers()
    return {
        "os": platform.os_name,
        "in_wsl": platform.in_wsl,
        "windows_interop": platform.windows_interop,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "browsers": [
            {"path": path, "needs_windows_path": needs_win} for path, needs_win in browsers
        ],
        "notes": platform.notes,
    }
