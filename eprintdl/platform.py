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
        local = os.environ.get("LOCALAPPDATA") or _windows_env("LOCALAPPDATA")
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


def to_wsl_path(path: str) -> str | None:
    text = str(path).strip()
    if shutil.which("wslpath"):
        try:
            result = subprocess.run(["wslpath", "-u", text], capture_output=True, text=True, timeout=15)
            converted = (result.stdout or "").strip()
            if result.returncode == 0 and converted:
                return converted
        except Exception:
            pass
    normalized = text.replace("\\", "/")
    if len(normalized) > 1 and normalized[1] == ":":
        return f"/mnt/{normalized[0].lower()}{normalized[2:]}"
    return None


def _windows_env(name: str) -> str | None:
    if shutil.which("cmd.exe") is None:
        return None
    try:
        result = subprocess.run(
            ["cmd.exe", "/c", "echo", f"%{name}%"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        raw = (result.stdout or "").strip()
    except Exception:
        return None
    if not raw or "%" in raw:
        return None
    return to_wsl_path(raw)


def wsl_networking_mode() -> str:
    if not is_wsl():
        return "none"
    profile = _windows_env("USERPROFILE")
    if not profile:
        return "unknown"
    try:
        with open(os.path.join(profile, ".wslconfig"), "r", encoding="utf-8-sig", errors="replace") as handle:
            for raw in handle:
                line = raw.split("#", 1)[0].strip().lower()
                if line.startswith("networkingmode") and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"') or "unknown"
    except OSError:
        return "nat"
    return "nat"


def find_windows_pythons() -> list[str]:
    if os.name == "nt" or not is_wsl() or shutil.which("cmd.exe") is None:
        return []
    found: list[str] = []

    def add(path: str | None) -> None:
        if path and os.path.isfile(path) and "windowsapps" not in path.lower() and path not in found:
            found.append(path)

    for name in ("python", "python3", "py"):
        try:
            result = subprocess.run(["cmd.exe", "/c", "where", name], capture_output=True, text=True, timeout=20)
        except Exception:
            continue
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.lower().endswith(".exe"):
                add(to_wsl_path(line))
    roots: list[str] = []
    local = _windows_env("LOCALAPPDATA")
    if local:
        roots.append(os.path.join(local, "Programs", "Python"))
    roots.extend(["/mnt/c/Program Files", "/mnt/c"])
    for root in roots:
        try:
            names = sorted(os.listdir(root), reverse=True)
        except OSError:
            continue
        for name in names:
            if name.lower().startswith("python3"):
                add(os.path.join(root, name, "python.exe"))
    return found


WSL_LOOPBACK_HINT = (
    "WSL 正在使用 Windows 版浏览器，而 WSL2 的 NAT 网络模式下 Linux 进程连不上它的调试端口。"
    "可选做法：在 WSL 内安装 Linux 版浏览器（例如 sudo apt install chromium）；"
    "或在 Windows 用户目录的 .wslconfig 中 [wsl2] 下加 networkingMode=mirrored 并执行 wsl --shutdown；"
    "或改用 Windows 版 Python 或 uvx.exe 运行本工具。"
)


def browser_preflight(browser_path: str, needs_windows_path: bool, user_data_dir: str) -> list[str]:
    problems: list[str] = []
    platform = detect_platform()
    if needs_windows_path:
        if wsl_networking_mode() != "mirrored":
            problems.append(WSL_LOOPBACK_HINT)
        return problems
    if platform.os_name == "linux" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        problems.append(
            "当前没有图形界面（DISPLAY 与 WAYLAND_DISPLAY 都为空），浏览器无法打开窗口。"
            "请在桌面会话中运行；无头模式容易被站点验证拦截，不建议使用。"
        )
    real = os.path.realpath(browser_path)
    if real.startswith("/snap/") or browser_path.startswith("/snap/"):
        home = os.path.expanduser("~")
        if not os.path.abspath(user_data_dir).startswith(home + os.sep):
            problems.append(
                f"检测到 snap 版浏览器，它只能访问家目录内的文件，而数据目录 {user_data_dir} 在家目录之外。"
                "请把 eprintdl.toml 里的 base_dir 改到家目录下，或安装非 snap 版的 Chrome。"
            )
    return problems


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
