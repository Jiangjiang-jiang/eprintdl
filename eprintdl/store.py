from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    paper_id     TEXT PRIMARY KEY,      -- 例如 "2024/1000"
    id_year      INTEGER NOT NULL,      -- 归档编号里的年份（权威归属年）
    year         INTEGER NOT NULL,      -- 同 id_year，供筛选使用
    number       INTEGER NOT NULL,
    title        TEXT,
    authors      TEXT,                  -- JSON 数组
    categories   TEXT,                  -- JSON 数组
    abstract     TEXT,
    rights       TEXT,
    published    TEXT,                  -- 首次发布日期（ISO）
    updated      TEXT,                  -- 最近更新日期（ISO）
    url          TEXT,
    pdf_url      TEXT,
    meta_source  TEXT,                  -- oai / browse
    meta_synced  TEXT,                  -- 元数据最近同步时间

    status       TEXT NOT NULL DEFAULT 'pending',  -- pending/done/skipped/failed
    file_path    TEXT,
    file_size    INTEGER,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    downloaded   TEXT,
    sha256       TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(year);
CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    paper_id TEXT,
    kind    TEXT NOT NULL,
    detail  TEXT
);
"""


class Store:

    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(papers)")}
        if "id_year" not in columns:
            self._conn.execute("ALTER TABLE papers ADD COLUMN id_year INTEGER")
            self._conn.execute(
                "UPDATE papers SET id_year = CAST(substr(paper_id, 1, instr(paper_id, '/') - 1) AS INTEGER)"
            )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_idyear ON papers(id_year)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            self._conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    def log_event(self, kind: str, paper_id: str | None = None, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(ts, paper_id, kind, detail) VALUES(?,?,?,?)",
                (time.strftime("%Y-%m-%dT%H:%M:%S"), paper_id, kind, detail),
            )
            self._conn.commit()

    def upsert_metadata(self, records: Iterable[dict]) -> int:
        rows = list(records)
        if not rows:
            return 0
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        sql = """
        INSERT INTO papers (paper_id, id_year, year, number, title, authors, categories, abstract,
                            rights, published, updated, url, pdf_url, meta_source, meta_synced,
                            status)
        VALUES (:paper_id, :id_year, :id_year, :number, :title, :authors, :categories, :abstract,
                :rights, :published, :updated, :url, :pdf_url, :meta_source, :meta_synced,
                'pending')
        ON CONFLICT(paper_id) DO UPDATE SET
            id_year     = excluded.id_year,
            year        = excluded.year,
            title       = COALESCE(excluded.title, papers.title),
            authors     = CASE WHEN excluded.authors NOT IN ('[]', '') THEN excluded.authors ELSE papers.authors END,
            categories  = CASE WHEN excluded.categories NOT IN ('[]', '') THEN excluded.categories ELSE papers.categories END,
            abstract    = COALESCE(excluded.abstract, papers.abstract),
            rights      = COALESCE(excluded.rights, papers.rights),
            published   = COALESCE(excluded.published, papers.published),
            updated     = COALESCE(excluded.updated, papers.updated),
            url         = COALESCE(excluded.url, papers.url),
            pdf_url     = COALESCE(excluded.pdf_url, papers.pdf_url),
            meta_source = excluded.meta_source,
            meta_synced = excluded.meta_synced
        """
        payload = []
        for rec in rows:
            paper_id = rec["paper_id"]
            year, _, number = paper_id.partition("/")
            payload.append(
                {
                    "paper_id": paper_id,
                    "id_year": int(year),
                    "number": int(number),
                    "title": rec.get("title"),
                    "authors": json.dumps(rec.get("authors") or [], ensure_ascii=False),
                    "categories": json.dumps(rec.get("categories") or [], ensure_ascii=False),
                    "abstract": rec.get("abstract"),
                    "rights": rec.get("rights"),
                    "published": rec.get("published"),
                    "updated": rec.get("updated"),
                    "url": rec.get("url"),
                    "pdf_url": rec.get("pdf_url"),
                    "meta_source": rec.get("meta_source", "oai"),
                    "meta_synced": now,
                }
            )
        with self._lock:
            self._conn.executemany(sql, payload)
            self._conn.commit()
        return len(payload)

    def mark_done(
        self, paper_id: str, file_path: str, size: int, sha256: str | None = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE papers SET status='done', file_path=?, file_size=?, sha256=?, "
                "downloaded=?, last_error=NULL WHERE paper_id=?",
                (file_path, size, sha256, time.strftime("%Y-%m-%dT%H:%M:%S"), paper_id),
            )
            self._conn.commit()

    def mark_failed(self, paper_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE papers SET status='failed', attempts=attempts+1, last_error=? "
                "WHERE paper_id=?",
                (error[:500], paper_id),
            )
            self._conn.commit()

    def mark_pending(self, paper_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE papers SET status='pending' WHERE paper_id=?", (paper_id,)
            )
            self._conn.commit()

    def bump_attempt(self, paper_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE papers SET attempts=attempts+1 WHERE paper_id=?", (paper_id,)
            )
            self._conn.commit()

    def query(
        self,
        *,
        years: list[int] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        categories: list[str] | None = None,
        keyword: str | None = None,
        status: list[str] | None = None,
        ids: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: str = "oldest",
    ) -> list[sqlite3.Row]:
        where: list[str] = []
        params: list[Any] = []
        if ids:
            where.append(f"paper_id IN ({','.join('?' * len(ids))})")
            params += ids
        if years:
            where.append(f"id_year IN ({','.join('?' * len(years))})")
            params += years
        if year_from is not None:
            where.append("id_year >= ?")
            params.append(year_from)
        if year_to is not None:
            where.append("id_year <= ?")
            params.append(year_to)
        if categories:
            clause = " OR ".join("categories LIKE ?" for _ in categories)
            where.append(f"({clause})")
            params += [f"%{c}%" for c in categories]
        if keyword:
            clause = (
                "(title LIKE ? OR authors LIKE ? OR abstract LIKE ? OR categories LIKE ?)"
            )
            where.append(clause)
            params += [f"%{keyword}%"] * 4
        if status:
            where.append(f"status IN ({','.join('?' * len(status))})")
            params += status
        direction = "DESC" if order == "newest" else "ASC"
        sql = "SELECT * FROM papers"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY year {direction}, number {direction}"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM papers GROUP BY status"
            ).fetchall()
            total = self._conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"]
            years = self._conn.execute("SELECT COUNT(DISTINCT year) AS n FROM papers").fetchone()["n"]
        out = {row["status"]: row["n"] for row in rows}
        out["total"] = total
        out["years"] = years
        return out

    def years_present(self) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT id_year FROM papers ORDER BY id_year"
            ).fetchall()
        return [row["id_year"] for row in rows]

    def known_ids(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT paper_id FROM papers").fetchall()
        return {row["paper_id"] for row in rows}
