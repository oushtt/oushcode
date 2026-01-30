from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


@dataclass(frozen=True)
class Job:
    id: int
    created_at: str
    updated_at: str
    kind: str
    status: str
    payload: dict[str, Any]
    repo: str | None
    issue_number: int | None
    pr_number: int | None
    head_sha: str | None
    iter: int


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def connect(db_path: str) -> sqlite3.Connection:
    _ensure_parent(db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deliveries (
            delivery_id TEXT PRIMARY KEY,
            received_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            repo TEXT,
            issue_number INTEGER,
            pr_number INTEGER,
            head_sha TEXT,
            iter INTEGER NOT NULL DEFAULT 0,
            delivery_id TEXT,
            error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS iterations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL,
            issue_number INTEGER,
            pr_number INTEGER,
            iter INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_keys (
            repo TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            head_sha TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (repo, pr_number, head_sha)
        )
        """
    )
    conn.commit()


def delivery_seen(conn: sqlite3.Connection, delivery_id: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM deliveries WHERE delivery_id = ? LIMIT 1", (delivery_id,)
    )
    return cur.fetchone() is not None


def mark_delivery(conn: sqlite3.Connection, delivery_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO deliveries (delivery_id, received_at) VALUES (?, ?)",
        (delivery_id, _utcnow()),
    )
    conn.commit()


def review_seen(conn: sqlite3.Connection, repo: str, pr_number: int, head_sha: str) -> bool:
    cur = conn.execute(
        """
        SELECT 1 FROM review_keys
        WHERE repo = ? AND pr_number = ? AND head_sha = ?
        LIMIT 1
        """,
        (repo, pr_number, head_sha),
    )
    return cur.fetchone() is not None


def mark_review(conn: sqlite3.Connection, repo: str, pr_number: int, head_sha: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO review_keys (repo, pr_number, head_sha, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (repo, pr_number, head_sha, _utcnow()),
    )
    conn.commit()


def enqueue_job(
    conn: sqlite3.Connection,
    *,
    kind: str,
    payload: dict[str, Any],
    repo: str | None = None,
    issue_number: int | None = None,
    pr_number: int | None = None,
    head_sha: str | None = None,
    delivery_id: str | None = None,
) -> int:
    now = _utcnow()
    cur = conn.execute(
        """
        INSERT INTO jobs (
            created_at, updated_at, status, kind, payload,
            repo, issue_number, pr_number, head_sha, iter, delivery_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now,
            now,
            "queued",
            kind,
            json.dumps(payload, ensure_ascii=True),
            repo,
            issue_number,
            pr_number,
            head_sha,
            0,
            delivery_id,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def fetch_next_job(conn: sqlite3.Connection) -> Job | None:
    cur = conn.execute(
        """
        SELECT * FROM jobs
        WHERE status = 'queued'
        ORDER BY id ASC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    if row is None:
        return None
    return Job(
        id=row["id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        kind=row["kind"],
        status=row["status"],
        payload=json.loads(row["payload"]),
        repo=row["repo"],
        issue_number=row["issue_number"],
        pr_number=row["pr_number"],
        head_sha=row["head_sha"],
        iter=row["iter"],
    )


def update_job_status(conn: sqlite3.Connection, job_id: int, status: str, error: str | None = None) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET status = ?, updated_at = ?, error = ?
        WHERE id = ?
        """,
        (status, _utcnow(), error, job_id),
    )
    conn.commit()


def set_iteration_status(
    conn: sqlite3.Connection,
    *,
    repo: str,
    issue_number: int | None,
    pr_number: int | None,
    iter_num: int,
    status: str,
) -> None:
    conn.execute(
        """
        INSERT INTO iterations (repo, issue_number, pr_number, iter, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (repo, issue_number, pr_number, iter_num, status, _utcnow()),
    )
    conn.commit()


def list_jobs(conn: sqlite3.Connection, status: str | None = None) -> Iterable[Job]:
    if status:
        cur = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY id ASC", (status,)
        )
    else:
        cur = conn.execute("SELECT * FROM jobs ORDER BY id ASC")
    for row in cur.fetchall():
        yield Job(
            id=row["id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            kind=row["kind"],
            status=row["status"],
            payload=json.loads(row["payload"]),
            repo=row["repo"],
            issue_number=row["issue_number"],
            pr_number=row["pr_number"],
            head_sha=row["head_sha"],
            iter=row["iter"],
        )


def get_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    cur = conn.execute("SELECT * FROM jobs WHERE id = ? LIMIT 1", (job_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return Job(
        id=row["id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        kind=row["kind"],
        status=row["status"],
        payload=json.loads(row["payload"]),
        repo=row["repo"],
        issue_number=row["issue_number"],
        pr_number=row["pr_number"],
        head_sha=row["head_sha"],
        iter=row["iter"],
    )
