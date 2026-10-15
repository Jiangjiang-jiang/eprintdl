from __future__ import annotations

import json
import os
import sys
from typing import Any

SCHEMA_VERSION = 1


def _now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Reporter:

    def __init__(self, mode: str = "human", *, stream=None, err_stream=None) -> None:
        self.mode = mode
        self.out = stream if stream is not None else sys.stdout
        self.err = err_stream if err_stream is not None else sys.stderr
        self._buffered: list[dict] = []

    @property
    def machine(self) -> bool:
        return self.mode in ("ndjson", "json")

    def _write_line(self, text: str) -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def event(self, kind: str, **payload: Any) -> dict:
        record = {"ok": payload.pop("ok", True), "v": SCHEMA_VERSION, "event": kind}
        record.update(payload)
        if self.mode == "ndjson":
            self._write_line(json.dumps(record, ensure_ascii=False))
        elif self.mode == "json":
            self._buffered.append(record)
        return record

    def say(self, text: str, *, quiet: bool = False) -> None:
        if quiet:
            return
        if self.mode == "human":
            self._write_line(text)
        else:
            self.err.write(text + "\n")
            self.err.flush()

    def warn(self, text: str) -> None:
        if self.mode == "human":
            self.err.write(text + "\n")
        else:
            self.err.write(text + "\n")
        self.err.flush()

    def finish(self, kind: str = "result", **payload: Any) -> dict:
        if self.mode == "json":
            document = {
                "ok": payload.pop("ok", True),
                "v": SCHEMA_VERSION,
                "finished": _now(),
                "events": self._buffered,
            }
            document.update(payload)
            self._write_line(json.dumps(document, ensure_ascii=False, indent=None))
            return document
        return self.event(kind, **payload)


def resolve_mode(args: Any) -> str:
    explicit = getattr(args, "format", None)
    if explicit:
        return explicit
    if getattr(args, "json", False):
        return "ndjson"
    return "human"


def relativize(path: str | None, base: str | None = None) -> str | None:
    if not path:
        return path
    try:
        return os.path.relpath(path, base) if base else path
    except ValueError:
        return path


def summary_line(**counts: Any) -> str:
    parts = [f"{key}={value}" for key, value in counts.items() if value is not None]
    return "SUMMARY " + " ".join(parts)
