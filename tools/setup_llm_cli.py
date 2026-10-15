from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eprintdl.platform import find_windows_pythons, is_wsl, resolve_browser, wsl_networking_mode

SERVER_MODULE = "eprintdl.mcp_server"
SERVER_ARGS = ["-X", "utf8", "-m", SERVER_MODULE]


def is_windows_interpreter_from_wsl(python: str) -> bool:
    return os.name != "nt" and python.lower().endswith(".exe")


def detect_python(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if os.name != "nt" and is_wsl():
        browser, browser_is_windows = resolve_browser()
        if browser and browser_is_windows and wsl_networking_mode() != "mirrored":
            candidates = find_windows_pythons()
            if candidates:
                return candidates[0]
            raise SystemExit(
                "WSL 下只找到了 Windows 版浏览器，服务端必须用 Windows 版 Python 运行，但没有找到它。"
                "请安装 Windows 版 Python，或用 --python 指定其路径（形如 /mnt/c/.../python.exe），"
                "或在 WSL 内安装 Linux 版浏览器后重新运行本脚本。"
            )
    for candidate in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return os.path.abspath(sys.executable)


def server_env(python: str) -> dict[str, str]:
    env = {"PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    config_file = ROOT / "eprintdl.toml"
    if config_file.is_file():
        env["EPRINTDL_CONFIG"] = str(config_file)
    if is_windows_interpreter_from_wsl(python):
        wslenv = ["PYTHONPATH/p", "PYTHONIOENCODING"]
        if "EPRINTDL_CONFIG" in env:
            wslenv.append("EPRINTDL_CONFIG/p")
        env["WSLENV"] = ":".join(wslenv)
    return env


def claude_config(python: str) -> dict:
    return {
        "mcpServers": {
            "eprintdl": {
                "type": "stdio",
                "command": python,
                "args": list(SERVER_ARGS),
                "env": server_env(python),
            }
        }
    }


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def codex_block(python: str) -> str:
    env_items = ", ".join(f"{_toml_string(k)} = {_toml_string(v)}" for k, v in server_env(python).items())
    args = ", ".join(_toml_string(a) for a in SERVER_ARGS)
    return (
        "[mcp_servers.eprintdl]\n"
        f"command = {_toml_string(python)}\n"
        f"args = [{args}]\n"
        f"env = {{ {env_items} }}\n"
        "startup_timeout_sec = 20.0\n"
        "tool_timeout_sec = 300.0\n"
    )


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def write_codex_user_config(block: str) -> Path:
    path = codex_home() / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    if text:
        shutil.copy2(path, path.with_name("config.toml.bak"))
    pattern = re.compile(r"(?ms)^\[mcp_servers\.eprintdl(?:\.[^\]]+)?\][^\n]*\n.*?(?=^\[|\Z)")
    cleaned = pattern.sub("", text).rstrip("\n")
    path.write_text((cleaned + "\n\n" if cleaned else "") + block, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Claude Code / Codex 的 MCP 配置")
    parser.add_argument("--print", dest="dry_run", action="store_true", help="只打印不写文件")
    parser.add_argument("--python", help="指定作为 MCP server 的解释器")
    parser.add_argument("--no-codex-user", action="store_true", help="不写入用户级 Codex 配置")
    args = parser.parse_args()

    python = detect_python(args.python)
    print(f"项目根    : {ROOT}")
    print(f"解释器    : {python}")
    print(f"server    : {SERVER_MODULE}")
    if not os.path.isfile(python):
        print(f"警告：解释器不存在 -> {python}")
    if is_windows_interpreter_from_wsl(python):
        print("说明      : WSL 下使用 Windows 版 Python，工具返回的 file_path 是 Windows 路径，在 WSL 中读取前用 wslpath -u 转换")
    print()

    claude_text = json.dumps(claude_config(python), ensure_ascii=False, indent=2) + "\n"
    block = codex_block(python)
    print("=== Claude Code: .mcp.json ===")
    print(claude_text)
    print("=== Codex: [mcp_servers.eprintdl] ===")
    print(block)

    if args.dry_run:
        print("（--print 模式，未写入文件）")
        return 0

    claude_path = ROOT / ".mcp.json"
    if claude_path.exists():
        shutil.copy2(claude_path, claude_path.with_suffix(".json.bak"))
    claude_path.write_text(claude_text, encoding="utf-8")
    print(f"已写入 {claude_path}")

    codex_project = ROOT / ".codex" / "config.toml"
    codex_project.parent.mkdir(parents=True, exist_ok=True)
    codex_project.write_text(block, encoding="utf-8")
    print(f"已写入 {codex_project}（仅当 Codex 信任本项目时生效）")

    if not args.no_codex_user:
        user_path = write_codex_user_config(block)
        print(f"已写入 {user_path}（用户级，不依赖项目信任；原文件已备份为 config.toml.bak）")

    print()
    print("下一步：")
    print("  1) 重启 Claude Code / Codex")
    print("  2) Claude Code 里用 /mcp，Codex 里用 codex mcp list，确认 eprintdl 已连接")
    print("  3) 自测：python tools/mcp_selftest.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
