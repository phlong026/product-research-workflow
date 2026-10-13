"""Web 层持久化:账号、会话、项目访问权限、作业、上传。独立 SQLite 文件,不与引擎库争用写锁。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from research_workflow.domain.models import utc_now_iso

from . import security

ROLES = ("admin", "researcher")
PROJECT_ROLES = ("owner", "editor", "viewer")
ACTIVE_JOB_STATUSES = ("queued", "running")


class WebStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin','researcher')),
                    disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_digest TEXT PRIMARY KEY, user_id TEXT NOT NULL, csrf_token TEXT NOT NULL,
                    expires_at REAL NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS project_access (
                    project_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('owner','editor','viewer')),
                    PRIMARY KEY (project_id, user_id));
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, command TEXT NOT NULL, params_json TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1, result_json TEXT, error TEXT,
                    created_by TEXT NOT NULL, retry_of TEXT, created_at TEXT NOT NULL,
                    started_at TEXT, finished_at TEXT);
                CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id, created_at);
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL, path TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL);
                """
            )

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self._lock, self._conn:
            return self._conn.execute(sql, params).rowcount

    # --- users / sessions ---
    def create_user(self, username: str, password: str, role: str) -> str:
        username = username.strip()
        if not username or role not in ROLES:
            raise ValueError("用户名不能为空,角色须为 admin 或 researcher")
        user_id = str(uuid4())
        try:
            self._write("INSERT INTO users (id, username, password_hash, role, created_at) VALUES (?,?,?,?,?)",
                        (user_id, username, security.hash_password(password), role, utc_now_iso()))
        except sqlite3.IntegrityError as exc:
            raise ValueError("用户名已存在") from exc
        return user_id

    def get_user_by_name(self, username: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM users WHERE username = ?", (username.strip(),))

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM users WHERE id = ?", (user_id,))

    def create_session(self, user_id: str, ttl: float = security.SESSION_TTL_SECONDS) -> tuple[str, str]:
        import time

        token, csrf = security.new_token(), security.new_token()
        self._write("INSERT INTO sessions VALUES (?,?,?,?,?)",
                    (security.token_digest(token), user_id, csrf, time.time() + ttl, utc_now_iso()))
        return token, csrf

    def get_session(self, token: str) -> dict[str, Any] | None:
        import time

        row = self._one("SELECT * FROM sessions WHERE token_digest = ?", (security.token_digest(token),))
        if row is None:
            return None
        if row["expires_at"] < time.time():
            self.delete_session(token)
            return None
        return row

    def delete_session(self, token: str) -> None:
        self._write("DELETE FROM sessions WHERE token_digest = ?", (security.token_digest(token),))

    # --- project access ---
    def grant(self, project_id: str, user_id: str, role: str) -> None:
        if role not in PROJECT_ROLES:
            raise ValueError("项目角色须为 owner/editor/viewer")
        self._write("INSERT INTO project_access VALUES (?,?,?) ON CONFLICT(project_id,user_id) DO UPDATE SET role=excluded.role",
                    (project_id, user_id, role))

    def project_role(self, project_id: str, user_id: str) -> str | None:
        row = self._one("SELECT role FROM project_access WHERE project_id=? AND user_id=?", (project_id, user_id))
        return row["role"] if row else None

    def project_ids_for(self, user_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT project_id, role FROM project_access WHERE user_id=?", (user_id,))

    # --- jobs ---
    def insert_job(self, *, project_id: str, command: str, params: dict[str, Any], created_by: str,
                   status: str = "queued", attempts: int = 1, retry_of: str | None = None) -> str:
        job_id = str(uuid4())
        self._write("INSERT INTO jobs (id, project_id, command, params_json, status, attempts, created_by, retry_of, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (job_id, project_id, command, json.dumps(params, ensure_ascii=False), status, attempts,
                     created_by, retry_of, utc_now_iso()))
        return job_id

    def claim_project_slot(self, *, project_id: str, command: str, params: dict[str, Any], created_by: str,
                           attempts: int = 1, retry_of: str | None = None) -> str | None:
        """原子地为项目登记作业;项目已有 queued/running 作业则返回 None(并发冲突)。"""
        with self._lock:
            active = self._conn.execute(
                "SELECT 1 FROM jobs WHERE project_id=? AND status IN ('queued','running') LIMIT 1", (project_id,)
            ).fetchone()
            if active:
                return None
            return self.insert_job(project_id=project_id, command=command, params=params, created_by=created_by,
                                   attempts=attempts, retry_of=retry_of)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM jobs WHERE id=?", (job_id,))
        return _job_view(row) if row else None

    def list_jobs(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM jobs WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                         (project_id, limit))
        return [_job_view(r) for r in rows]

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {"status", "result_json", "error", "started_at", "finished_at"}
        if not set(fields) <= allowed:
            raise ValueError("非法作业字段")
        assignments = ", ".join(f"{k}=?" for k in fields)
        self._write(f"UPDATE jobs SET {assignments} WHERE id=?", (*fields.values(), job_id))

    def fail_interrupted_jobs(self) -> int:
        return self._write(
            "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE status IN ('queued','running')",
            ("服务重启导致作业中断,可重试", utc_now_iso()))

    # --- uploads ---
    def add_upload(self, *, project_id: str, filename: str, sha256: str, size: int, path: str, created_by: str) -> str:
        upload_id = str(uuid4())
        self._write("INSERT INTO uploads VALUES (?,?,?,?,?,?,?,?)",
                    (upload_id, project_id, filename, sha256, size, path, created_by, utc_now_iso()))
        return upload_id

    def get_upload(self, project_id: str, upload_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM uploads WHERE id=? AND project_id=?", (upload_id, project_id))


def _job_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": row["id"], "project_id": row["project_id"], "command": row["command"],
        "params": json.loads(row["params_json"]), "status": row["status"], "attempts": row["attempts"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None, "error": row["error"],
        "created_by": row["created_by"], "retry_of": row["retry_of"], "created_at": row["created_at"],
        "started_at": row["started_at"], "finished_at": row["finished_at"],
    }
