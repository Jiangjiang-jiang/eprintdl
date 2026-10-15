from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", ".venv", "__pycache__", "eprint-data", ".chrome-profile"}
TEXT_EXT = {".py", ".md", ".toml", ".json", ".cmd", ".sh", ".txt", ".cfg", ".ini"}
BOM = b"\xef\xbb\xbf"
MOJIBAKE_CODEPOINTS = (
    0x950B, 0x94C2, 0x9428, 0x6D93, 0x93C1, 0x7F41, 0x92D9, 0x7EFA, 0x952F,
)
MOJIBAKE = tuple(chr(code) for code in MOJIBAKE_CODEPOINTS)


def walk_files() -> list[str]:
    out: list[str] = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if os.path.splitext(name)[1].lower() in TEXT_EXT:
                out.append(os.path.join(base, name))
    return sorted(out)


def main() -> int:
    problems = 0
    checked = 0
    for path in walk_files():
        rel = os.path.relpath(path, ROOT)
        raw = open(path, "rb").read()
        checked += 1

        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            print(f"[损坏] {rel}: 不是合法 UTF-8 -> {exc}")
            problems += 1
            continue

        if "\ufffd" in text:
            count = text.count("\ufffd")
            print(f"[损坏] {rel}: 含 {count} 个 U+FFFD 替换字符（编码转换已丢信息）")
            problems += 1

        hits = [m for m in MOJIBAKE if m in text]
        if hits:
            print(f"[乱码] {rel}: 命中特征 {hits}")
            problems += 1

        if path.endswith(".py"):
            try:
                ast.parse(text, path)
            except SyntaxError as exc:
                print(f"[语法] {rel}: {exc}")
                problems += 1

        if path.endswith(".cmd") or path.endswith(".bat"):
            try:
                raw.decode("ascii")
            except UnicodeDecodeError:
                print(f"[编码] {rel}: 批处理含非 ASCII 字符，cmd.exe 可能解析失败")
                problems += 1

        if raw.startswith(BOM):
            print(f"[BOM] {rel}")
            problems += 1

    print(f"\n检查 {checked} 个文本文件，问题 {problems} 处。")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
