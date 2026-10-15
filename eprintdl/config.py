from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

try:
    import tomllib as _toml
except ModuleNotFoundError:
    try:
        import tomli as _toml
    except ModuleNotFoundError:
        _toml = None

CONFIG_NAME = "eprintdl.toml"


def _parse_toml(text: str) -> dict:
    if _toml is not None:
        return _toml.loads(text)
    return _parse_simple_toml(text)


def _parse_simple_toml(text: str) -> dict:
    data: dict[str, Any] = {}
    section: dict[str, Any] = data
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            section = data.setdefault(name, {})
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().strip('"'), value.strip()
        if value.startswith('"') and value.endswith('"'):
            parsed: Any = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            parsed = value[1:-1]
        elif value.lower() in ("true", "false"):
            parsed = value.lower() == "true"
        else:
            try:
                parsed = int(value)
            except ValueError:
                try:
                    parsed = float(value)
                except ValueError:
                    parsed = value
        section[key] = parsed
    return data


def find_config_file(explicit: str | None = None, start_dir: str | None = None) -> str | None:
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    env = os.environ.get("EPRINTDL_CONFIG")
    if env and os.path.isfile(env):
        return env
    home = os.path.expanduser("~")
    user_level = os.path.join(home, CONFIG_NAME)
    if os.path.isfile(user_level):
        return user_level
    current = os.path.abspath(start_dir or os.getcwd())
    while True:
        candidate = os.path.join(current, CONFIG_NAME)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


@dataclass
class Config:

    config_file: str | None = None
    base_dir: str = ""
    db: str = ""
    out: str = ""
    profile: str = ""
    lock: str = ""
    rate: float = 1.0
    retries: int = 3
    timeout: float = 120.0
    title_len: int = 120
    chrome: str = ""
    port: int = 0
    headless: bool = False
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "config_file": self.config_file,
            "base_dir": self.base_dir,
            "db": self.db,
            "out": self.out,
            "profile": self.profile,
            "lock": self.lock,
            "rate": self.rate,
            "retries": self.retries,
            "timeout": self.timeout,
            "title_len": self.title_len,
            "chrome": self.chrome or None,
            "port": self.port,
            "headless": self.headless,
        }


def _is_windows_style_path(value: str) -> bool:
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return True
    return value.startswith("\\\\")


def _expand(value: str, base: str) -> str:
    text = str(value).strip().strip('"').strip("'")
    if _is_windows_style_path(text):
        return text
    expanded = os.path.expanduser(os.path.expandvars(text))
    if not os.path.isabs(expanded):
        expanded = os.path.join(base, expanded)
    return os.path.abspath(expanded)


def load_config(
    explicit: str | None = None,
    *,
    start_dir: str | None = None,
    overrides: dict | None = None,
) -> Config:
    path = find_config_file(explicit, start_dir)
    data: dict = {}
    if path:
        with open(path, "rb") as handle:
            raw = handle.read()
        try:
            data = _parse_toml(raw.decode("utf-8-sig"))
        except Exception as exc:
            raise SystemExit(f"配置文件解析失败 {path}: {exc}") from exc

    paths = data.get("paths", {}) or {}
    download = data.get("download", {}) or {}
    server = data.get("server", {}) or {}

    anchor = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()

    chrome_value = str(download.get("chrome", "") or "").strip().strip('"').strip("'")
    if chrome_value and not _is_windows_style_path(chrome_value):
        chrome_value = os.path.expanduser(os.path.expandvars(chrome_value))
        if not os.path.isabs(chrome_value):
            chrome_value = os.path.abspath(os.path.join(anchor, chrome_value))

    default_base = "eprint-data" if path else os.path.join(os.path.expanduser("~"), "eprint-data")
    base_dir = _expand(paths.get("base_dir", default_base), anchor)
    config = Config(
        config_file=path,
        base_dir=base_dir,
        db=_expand(paths.get("db", os.path.join(base_dir, "eprint.sqlite3")), anchor),
        out=_expand(paths.get("out", os.path.join(base_dir, "pdfs")), anchor),
        profile=_expand(
            paths.get("profile", os.path.join(base_dir, ".chrome-profile")), anchor
        ),
        lock=_expand(paths.get("lock", os.path.join(base_dir, ".chrome.lock")), anchor),
        rate=float(download.get("rate", 1.0)),
        retries=int(download.get("retries", 3)),
        timeout=float(download.get("timeout", 120.0)),
        title_len=int(download.get("title_len", 120)),
        chrome=chrome_value,
        port=int(server.get("port", 0)),
        headless=bool(download.get("headless", False)),
        extra={k: v for k, v in data.items() if k not in ("paths", "download", "server")},
    )

    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if key in ("db", "out", "profile", "lock"):
            setattr(config, key, _expand(value, anchor))
        elif key == "chrome":
            cleaned = str(value).strip().strip('"').strip("'")
            if cleaned and not _is_windows_style_path(cleaned) and not os.path.isabs(cleaned):
                cleaned = os.path.abspath(os.path.join(anchor, cleaned))
            setattr(config, key, cleaned)
        else:
            setattr(config, key, value)
    return config


def write_default_config(path: str) -> str:
    template = '''[paths]
base_dir = "eprint-data"

[download]
rate = 1.0
retries = 3
timeout = 120.0
title_len = 120
chrome = ""

[server]
port = 0
'''
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(template)
    return path


def python_executable() -> str:
    return os.path.abspath(sys.executable)
