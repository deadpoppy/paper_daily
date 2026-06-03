"""SQLite persistence for papers and recommendations."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doi TEXT UNIQUE,
    arxiv_id TEXT UNIQUE,
    title TEXT NOT NULL,
    authors TEXT,           -- JSON list
    abstract TEXT,
    year INTEGER,
    published_date TEXT,    -- YYYY-MM-DD
    venue TEXT,
    citation_count INTEGER DEFAULT 0,
    url TEXT,
    sources TEXT,           -- comma-separated
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    recommend_date TEXT NOT NULL,
    rank INTEGER NOT NULL,
    score REAL,
    reason_zh TEXT,
    academic_score REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_arxiv ON papers(arxiv_id);
CREATE INDEX IF NOT EXISTS idx_papers_title ON papers(title);
CREATE INDEX IF NOT EXISTS idx_rec_paper ON recommendations(paper_id);
CREATE INDEX IF NOT EXISTS idx_rec_date ON recommendations(recommend_date);

CREATE TABLE IF NOT EXISTS academic_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT UNIQUE NOT NULL,
    arxiv_id TEXT,
    doi TEXT,
    title TEXT,
    academic_score REAL NOT NULL,
    academic_reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cache_hash ON academic_cache(content_hash);
CREATE INDEX IF NOT EXISTS idx_cache_arxiv ON academic_cache(arxiv_id);
CREATE INDEX IF NOT EXISTS idx_cache_doi ON academic_cache(doi);
"""


class PaperDatabase:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # Migrate: add academic_score if missing (for existing DBs)
            try:
                conn.execute("SELECT academic_score FROM recommendations LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE recommendations ADD COLUMN academic_score REAL")
            # Migrate: add summary_md_path if missing (for existing DBs)
            try:
                conn.execute("SELECT summary_md_path FROM papers LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE papers ADD COLUMN summary_md_path TEXT")
            conn.commit()

    # ------------------------------------------------------------------
    # Papers
    # ------------------------------------------------------------------
    def get_seen_dois(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT p.doi FROM papers p
                   JOIN recommendations r ON r.paper_id = p.id
                   WHERE p.doi IS NOT NULL"""
            ).fetchall()
            return {r["doi"].lower() for r in rows}

    def get_seen_arxiv_ids(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT p.arxiv_id FROM papers p
                   JOIN recommendations r ON r.paper_id = p.id
                   WHERE p.arxiv_id IS NOT NULL"""
            ).fetchall()
            return {r["arxiv_id"].lower() for r in rows}

    def get_seen_titles(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT p.title FROM papers p
                   JOIN recommendations r ON r.paper_id = p.id"""
            ).fetchall()
            return {r["title"].lower().strip().rstrip(".") for r in rows}

    def paper_exists(self, doi: str | None, arxiv_id: str | None, title: str) -> bool:
        norm_title = title.lower().strip().rstrip(".")
        with self._connect() as conn:
            if doi:
                row = conn.execute("SELECT 1 FROM papers WHERE doi = ?", (doi,)).fetchone()
                if row:
                    return True
            if arxiv_id:
                row = conn.execute("SELECT 1 FROM papers WHERE arxiv_id = ?", (arxiv_id,)).fetchone()
                if row:
                    return True
            row = conn.execute(
                "SELECT 1 FROM papers WHERE lower(trim(rtrim(title,'.'))) = ?",
                (norm_title,),
            ).fetchone()
            return row is not None

    def insert_paper(self, paper: dict[str, Any]) -> int:
        with self._connect() as conn:
            # 1) 先按 doi 或 arxiv_id 查找已有记录
            existing_id = None
            if paper.get("doi"):
                row = conn.execute(
                    "SELECT id FROM papers WHERE doi = ?", (paper["doi"],)
                ).fetchone()
                if row:
                    existing_id = row["id"]
            if existing_id is None and paper.get("arxiv_id"):
                row = conn.execute(
                    "SELECT id FROM papers WHERE arxiv_id = ?", (paper["arxiv_id"],)
                ).fetchone()
                if row:
                    existing_id = row["id"]

            if existing_id is not None:
                # 2) 更新已有记录（保留更优/更全的字段）
                conn.execute(
                    """
                    UPDATE papers SET
                        arxiv_id   = COALESCE(?, arxiv_id),
                        title      = ?,
                        authors    = ?,
                        abstract   = ?,
                        year       = ?,
                        published_date = ?,
                        venue      = COALESCE(?, venue),
                        citation_count = MAX(?, citation_count),
                        url        = COALESCE(?, url),
                        sources    = MAX(sources, ?)
                    WHERE id = ?
                    """,
                    (
                        paper.get("arxiv_id") or None,
                        paper.get("title", ""),
                        json.dumps(paper.get("authors", []), ensure_ascii=False),
                        paper.get("abstract", ""),
                        paper.get("year"),
                        paper.get("published_date"),
                        paper.get("venue", ""),
                        paper.get("citation_count", 0) or 0,
                        paper.get("url", ""),
                        paper.get("source", ""),
                        existing_id,
                    ),
                )
                conn.commit()
                return existing_id

            # 3) 不存在则插入新记录
            cur = conn.execute(
                """
                INSERT INTO papers (doi, arxiv_id, title, authors, abstract, year,
                                    published_date, venue, citation_count, url, sources)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    paper.get("doi") or None,
                    paper.get("arxiv_id") or None,
                    paper.get("title", ""),
                    json.dumps(paper.get("authors", []), ensure_ascii=False),
                    paper.get("abstract", ""),
                    paper.get("year"),
                    paper.get("published_date"),
                    paper.get("venue", ""),
                    paper.get("citation_count", 0) or 0,
                    paper.get("url", ""),
                    paper.get("source", ""),
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return row["id"]

    def get_paper_by_doi(self, doi: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
            return dict(row) if row else None

    def update_paper_summary_md_path(self, arxiv_id: str, md_path: str) -> None:
        """Update the summary_md_path for a paper identified by arxiv_id."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE papers SET summary_md_path = ? WHERE arxiv_id = ?",
                (md_path, arxiv_id),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Recommendations
    # ------------------------------------------------------------------
    def insert_recommendation(
        self, paper_id: int, rank: int, score: float, reason_zh: str, academic_score: float | None = None
    ) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO recommendations (paper_id, recommend_date, rank, score, reason_zh, academic_score)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (paper_id, today, rank, score, reason_zh, academic_score),
            )
            conn.commit()

    def get_recent_recommendations(self, days: int = 30) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT r.*, p.title, p.doi, p.arxiv_id, p.url
                FROM recommendations r
                JOIN papers p ON p.id = r.paper_id
                WHERE r.recommend_date >= date('now', '-{} days')
                ORDER BY r.recommend_date DESC, r.rank ASC
                """.format(days),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_recommendations(self, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT r.*, p.title, p.doi, p.arxiv_id, p.url
                FROM recommendations r
                JOIN papers p ON p.id = r.paper_id
                ORDER BY r.recommend_date DESC, r.rank ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def is_recommended(self, paper_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM recommendations WHERE paper_id = ?", (paper_id,)
            ).fetchone()
            return row is not None

    # ------------------------------------------------------------------
    # Academic-value cache
    # ------------------------------------------------------------------
    def get_academic_cache(self, content_hash: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT academic_score, academic_reason FROM academic_cache WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
            return dict(row) if row else None

    def get_academic_cache_by_arxiv_id(self, arxiv_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT academic_score, academic_reason FROM academic_cache WHERE arxiv_id = ?",
                (arxiv_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_academic_cache_by_doi(self, doi: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT academic_score, academic_reason FROM academic_cache WHERE doi = ?",
                (doi,),
            ).fetchone()
            return dict(row) if row else None

    def set_academic_cache(
        self,
        content_hash: str,
        arxiv_id: str | None,
        doi: str | None,
        title: str,
        score: float,
        reason: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO academic_cache (content_hash, arxiv_id, doi, title, academic_score, academic_reason)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(content_hash) DO UPDATE SET
                    arxiv_id=COALESCE(excluded.arxiv_id, arxiv_id),
                    doi=COALESCE(excluded.doi, doi),
                    academic_score=excluded.academic_score,
                    academic_reason=excluded.academic_reason,
                    created_at=CURRENT_TIMESTAMP
                """,
                (content_hash, arxiv_id, doi, title, score, reason),
            )
            conn.commit()
