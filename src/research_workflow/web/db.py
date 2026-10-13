"""工作台自己的 SQLite：用户、项目成员、后台任务、审计日志。

与引擎研究库（research.db）分离：引擎状态仍由 CLI/WorkflowService 独占写入，
工作台只通过任务队列调用 CLI，因此引擎的审批哈希绑定与事务串行保证原样保留。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROLES = ("admin", "member")
PROJECT_ROLES = ("owner", "editor", "viewer")
_ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_token(token: str) -> str:
    # 令牌是 256 位随机数，不需要慢哈希；只存哈希，明文仅在创建时显示一次。
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class WorkbenchStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def _migrate(self) -> None:
        with self._tx() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, role TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE, disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS project_members (
                    project_id TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(id),
                    role TEXT NOT NULL, PRIMARY KEY (project_id, user_id));
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, command TEXT NOT NULL, args_json TEXT NOT NULL,
                    status TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
                    started_at TEXT, finished_at TEXT, returncode INTEGER, result_json TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, filename TEXT NOT NULL, path TEXT NOT NULL,
                    size INTEGER NOT NULL, sha256 TEXT NOT NULL, uploaded_by TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, user_name TEXT NOT NULL,
                    project_id TEXT, action TEXT NOT NULL, detail_json TEXT NOT NULL);
                """
            )

    # --- 用户 ---
    def create_user(self, name: str, role: str) -> tuple[dict[str, Any], str]:
        if role not in ROLES:
            raise ValueError(f"role 必须是 {ROLES}")
        if not name.strip() or len(name) > 64:
            raise ValueError("用户名需 1-64 字符")
        token = secrets.token_urlsafe(32)
        user_id = uuid.uuid4().hex
        with self._tx() as c:
            c.execute("INSERT INTO users (id,name,role,token_hash,created_at) VALUES (?,?,?,?,?)",
                      (user_id, name.strip(), role, hash_token(token), now_iso()))
        return {"id": user_id, "name": name.strip(), "role": role}, token

    def user_by_token(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,name,role,disabled FROM users WHERE token_hash=?", (hash_token(token),)).fetchone()
        if row is None or row["disabled"]:
            return None
        return {"id": row["id"], "name": row["name"], "role": row["role"]}

    def list_users(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT id,name,role,disabled,created_at FROM users ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def set_disabled(self, name: str, disabled: bool) -> None:
        with self._tx() as c:
            if c.execute("UPDATE users SET disabled=? WHERE name=?", (int(disabled), name)).rowcount == 0:
                raise KeyError(name)

    def rotate_token(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._tx() as c:
            if c.execute("UPDATE users SET token_hash=? WHERE name=?", (hash_token(token), name)).rowcount == 0:
                raise KeyError(name)
        return token

    def user_by_name(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT id,name,role,disabled FROM users WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    # --- 项目成员 ---
    def add_member(self, project_id: str, user_id: str, role: str) -> None:
        if role not in PROJECT_ROLES:
            raise ValueError(f"项目角色必须是 {PROJECT_ROLES}")
        with self._tx() as c:
            c.execute("INSERT INTO project_members (project_id,user_id,role) VALUES (?,?,?) "
                      "ON CONFLICT(project_id,user_id) DO UPDATE SET role=excluded.role", (project_id, user_id, role))

    def remove_member(self, project_id: str, user_id: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM project_members WHERE project_id=? AND user_id=?", (project_id, user_id))

    def members(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.id,u.name,m.role FROM project_members m JOIN users u ON u.id=m.user_id "
                "WHERE m.project_id=? ORDER BY u.name", (project_id,)).fetchall()
        return [dict(r) for r in rows]

    def member_role(self, project_id: str, user: dict[str, Any]) -> str | None:
        """admin 对所有项目拥有 owner 权限；其他人取成员角色，非成员返回 None。"""
        if user["role"] == "admin":
            return "owner"
        with self._lock:
            row = self._conn.execute("SELECT role FROM project_members WHERE project_id=? AND user_id=?",
                                     (project_id, user["id"])).fetchone()
        return row["role"] if row else None

    def projects_of(self, user: dict[str, Any]) -> list[str] | None:
        if user["role"] == "admin":
            return None  # None = 全部
        with self._lock:
            rows = self._conn.execute("SELECT project_id FROM project_members WHERE user_id=?", (user["id"],)).fetchall()
        return [r["project_id"] for r in rows]

    @staticmethod
    def at_least(role: str | None, needed: str) -> bool:
        return role is not None and _ROLE_RANK[role] >= _ROLE_RANK[needed]

    # --- 上传 ---
    def add_upload(self, *, project_id: str, filename: str, path: str, size: int, sha256: str, user: str, upload_id: str) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO uploads VALUES (?,?,?,?,?,?,?,?)",
                      (upload_id, project_id, filename, path, size, sha256, user, now_iso()))

    def get_upload(self, project_id: str, upload_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM uploads WHERE id=? AND project_id=?", (upload_id, project_id)).fetchone()
        return dict(row) if row else None

    def list_uploads(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT id,filename,size,sha256,uploaded_by,created_at FROM uploads "
                                      "WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()
        return [dict(r) for r in rows]

    # --- 任务 ---
    def enqueue(self, *, project_id: str, command: str, args: dict[str, Any], user: str, job_id: str | None = None) -> str:
        job_id = job_id or uuid.uuid4().hex
        with self._tx() as c:
            c.execute("INSERT INTO jobs (id,project_id,command,args_json,status,created_by,created_at) VALUES (?,?,?,?,?,?,?)",
                      (job_id, project_id, command, json.dumps(args, ensure_ascii=False), "queued", user, now_iso()))
        return job_id

    def claim_next(self) -> dict[str, Any] | None:
        with self._tx() as c:
            row = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at, rowid LIMIT 1").fetchone()
            if row is None:
                return None
            c.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?", (now_iso(), row["id"]))
        return dict(row)

    def finish(self, job_id: str, *, returncode: int | None, result: Any, error: str | None) -> None:
        with self._tx() as c:
            c.execute("UPDATE jobs SET status=?, finished_at=?, returncode=?, result_json=?, error=? WHERE id=?",
                      ("succeeded" if error is None else "failed", now_iso(), returncode,
                       json.dumps(result, ensure_ascii=False) if result is not None else None, error, job_id))

    def interrupt_running(self) -> int:
        """进程重启时：上次未结束的任务不自动重跑（可能已产生部分副作用），标记失败由人确认后重提。"""
        with self._tx() as c:
            return c.execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE status='running'",
                             (now_iso(), "服务重启时任务被中断，未自动重跑；请检查项目状态后重新提交")).rowcount

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job_row(row) if row else None

    def list_jobs(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jobs WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                      (project_id, limit)).fetchall()
        return [_job_row(r) for r in rows]

    # --- 审计 ---
    def audit(self, actor: str, action: str, project_id: str | None = None, **detail: Any) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO audit_log (at,user_name,project_id,action,detail_json) VALUES (?,?,?,?,?)",
                      (now_iso(), actor, project_id, action, json.dumps(detail, ensure_ascii=False, default=str)))

    def audit_log(self, project_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT at,user_name,action,detail_json FROM audit_log WHERE project_id=? "
                                      "ORDER BY id DESC LIMIT ?", (project_id, limit)).fetchall()
        return [{"at": r["at"], "user": r["user_name"], "action": r["action"], "detail": json.loads(r["detail_json"])} for r in rows]


def _job_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["args"] = json.loads(d.pop("args_json"))
    d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
    d.pop("result_json", None)
    return d
