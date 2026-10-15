from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_MODULE = "eprintdl.mcp_server"

SERVER_ARGS = ["-X", "utf8", "-m", SERVER_MODULE]


def _is_windows_path(path: str) -> bool:
    return len(path) >= 2 and path[1] == ":" and path[0].isalpha()


def to_windows_path(path: str) -> str | None:
    if os.name == "nt":
        return path
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    if len(parts) > 3 and parts[1] == "mnt" and len(parts[2]) == 1:
        rest = "\\".join(parts[3:])
        return f"{parts[2].upper()}:\\{rest}" if rest else f"{parts[2].upper()}:\\"
    return None


def detect_python(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    for candidate in (
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
    ):
        if candidate.is_file():
            return str(candidate)
    return os.path.abspath(sys.executable)


def project_path_for(python: str) -> str:
    if os.name == "nt":
        return str(ROOT)
    if _is_windows_path(python):
        converted = to_windows_path(str(ROOT))
        if converted:
            return converted
    return str(ROOT)


def is_windows_interpreter_from_wsl(python: str) -> bool:
    return os.name != "nt" and python.lower().endswith(".exe")


def server_env(python: str, pythonpath: str) -> dict[str, str]:
    env = {"PYTHONPATH": pythonpath, "PYTHONIOENCODING": "utf-8"}
    config_file = ROOT / "eprintdl.toml"
    if config_file.is_file():
        env["EPRINTDL_CONFIG"] = str(config_file)
    if is_windows_interpreter_from_wsl(python):
        wslenv = ["PYTHONPATH/p", "PYTHONIOENCODING"]
        if "EPRINTDL_CONFIG" in env:
            wslenv.append("EPRINTDL_CONFIG/p")
        env["WSLENV"] = ":".join(wslenv)
    return env


def claude_config(python: str, pythonpath: str) -> dict:
    return {
        "mcpServers": {
            "eprintdl": {
                "type": "stdio",
                "command": python,
                "args": list(SERVER_ARGS),
                "env": server_env(python, pythonpath),
            }
        }
    }


def codex_config(python: str, pythonpath: str) -> str:
    esc_python = python.replace("\\", "\\\\")
    env_items = ", ".join(
        f'"{key}" = "{value.replace(chr(92), chr(92) * 2)}"'
        for key, value in server_env(python, pythonpath).items()
    )
    return f"""[mcp_servers.eprintdl]
command = "{esc_python}"
args = ["-X", "utf8", "-m", "eprintdl.mcp_server"]
env = {{ {env_items} }}
startup_timeout_sec = 20.0
tool_timeout_sec = 300.0
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Claude Code / Codex 的 MCP 配置")
    parser.add_argument("--print", dest="dry_run", action="store_true", help="只打印不写文件")
    parser.add_argument("--python", help="指定作为 MCP server 的解释器")
    args = parser.parse_args()

    python = detect_python(args.python)
    pythonpath = project_path_for(python)
    print(f"项目根    : {ROOT}")
    print(f"解释器    : {python}")
    print(f"PYTHONPATH: {pythonpath}")
    print(f"server    : {SERVER_MODULE}")
    if not os.path.isfile(python):
        print(f"警告：解释器不存在 -> {python}")
    print()

    print("=== Claude Code: .mcp.json ===")
    print(json.dumps(claude_config(python, pythonpath), ensure_ascii=False, indent=2))
    print()
    print("=== Codex: .codex/config.toml ===")
    print(codex_config(python, pythonpath))

    if args.dry_run:
        print("（--print 模式，未写入文件）")
        return 0

    claude_path = ROOT / ".mcp.json"
    codex_path = ROOT / ".codex" / "config.toml"

    if claude_path.exists():
        backup = claude_path.with_suffix(".json.bak")
        print(f"注意：{claude_path} 已存在，备份为 {backup.name}")
        shutil.copy2(claude_path, backup)
    claude_path.write_text(
        json.dumps(claude_config(python, pythonpath), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    codex_path.parent.mkdir(parents=True, exist_ok=True)
    codex_path.write_text(codex_config(python, pythonpath), encoding="utf-8")

    print(f"已写入 {claude_path}")
    print(f"已写入 {codex_path}")
    print()
    print("下一步：")
    print("  1) 重启 Claude Code / Codex，让它们重新加载 MCP 配置")
    print("  2) Claude Code 里用 /mcp 确认 eprintdl 已连接")
    print("  3) 自测：python tools/mcp_selftest.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
