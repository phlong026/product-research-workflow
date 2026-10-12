from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from research_workflow.domain import (
    CheckpointStatus,
    HumanCheckpoint,
    ProjectStatus,
    ResearchProject,
    RunStatus,
    StepStatus,
    WorkflowRun,
    WorkflowStep,
)


class SQLiteStore:
    """Small persistence layer for the first development stage.

    SQLite is intentional for the minimal first version. Domain APIs isolate the
    storage choice so PostgreSQL can replace it later without changing research
    modules.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._transaction_depth = 0
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    @contextmanager
    def transaction(self, on_rollback=None):
        """Serialize a service transition and commit all state changes together."""
        with self._lock:
            outer = self._transaction_depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._transaction_depth += 1
            try:
                yield
                if outer:
                    self._conn.commit()
            except BaseException:
                if outer:
                    try:
                        if on_rollback is not None:
                            on_rollback()
                    finally:
                        self._conn.rollback()
                raise
            finally:
                self._transaction_depth -= 1

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: tuple = ()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            if not self._transaction_depth:
                self._conn.commit()
            return cur

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    product TEXT NOT NULL,
                    marketplace TEXT NOT NULL,
                    status TEXT NOT NULL,
                    primary_keyword TEXT,
                    main_node TEXT,
                    research_start TEXT,
                    research_end TEXT,
                    final_decision_owner TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_steps (
                    project_id TEXT NOT NULL,
                    step_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    output_ref TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, step_key),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS workflow_state (
                    project_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    step_key TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_checkpoints_project_status
                    ON checkpoints(project_id, status);

                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    graph_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    retry_of TEXT,
                    current_step TEXT NOT NULL,
                    state_payload TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    FOREIGN KEY(retry_of) REFERENCES workflow_runs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_runs_project
                    ON workflow_runs(project_id, created_at);
                CREATE TABLE IF NOT EXISTS workflow_run_steps (
                    run_id TEXT NOT NULL,
                    step_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    output_ref TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, step_key),
                    FOREIGN KEY(run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS workflow_resume_receipts (
                    run_id TEXT NOT NULL,
                    resume_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, resume_key),
                    FOREIGN KEY(run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
                );
                """
            )
            self._conn.commit()

    # --- Project-level persistence kept for compatibility with T01 checkpoint APIs. ---
    def create_project(self, project: ResearchProject) -> ResearchProject:
        values = asdict(project)
        values["status"] = project.status.value
        cols = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        self._execute(
            f"INSERT INTO projects ({cols}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        return project

    def get_project(self, project_id: str) -> ResearchProject | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["status"] = ProjectStatus(data["status"])
        return ResearchProject(**data)

    def list_projects(self) -> list[ResearchProject]:
        with self._lock:
            rows = self._conn.execute("SELECT id FROM projects ORDER BY created_at DESC").fetchall()
        return [project for row in rows if (project := self.get_project(row["id"])) is not None]

    def update_project_status(self, project_id: str, status: ProjectStatus, updated_at: str) -> None:
        self._execute(
            "UPDATE projects SET status=?, updated_at=? WHERE id=?",
            (status.value, updated_at, project_id),
        )

    def upsert_step(self, step: WorkflowStep) -> WorkflowStep:
        self._execute(
            """
            INSERT INTO workflow_steps(project_id,step_key,status,output_ref,error,updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(project_id,step_key) DO UPDATE SET
              status=excluded.status,
              output_ref=excluded.output_ref,
              error=excluded.error,
              updated_at=excluded.updated_at
            """,
            (
                step.project_id,
                step.step_key,
                step.status.value,
                step.output_ref,
                step.error,
                step.updated_at,
            ),
        )
        return step

    def list_steps(self, project_id: str) -> list[WorkflowStep]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_steps WHERE project_id=? ORDER BY rowid", (project_id,)
            ).fetchall()
        return [
            WorkflowStep(
                project_id=r["project_id"],
                step_key=r["step_key"],
                status=StepStatus(r["status"]),
                output_ref=r["output_ref"],
                error=r["error"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def put_state(self, project_id: str, payload: dict[str, Any]) -> None:
        self._execute(
            """
            INSERT INTO workflow_state(project_id,payload) VALUES(?,?)
            ON CONFLICT(project_id) DO UPDATE SET payload=excluded.payload
            """,
            (project_id, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )

    def get_state(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM workflow_state WHERE project_id=?", (project_id,)
            ).fetchone()
        return json.loads(row["payload"]) if row else {}

    # --- Run-level persistence. ---
    def create_run(self, run: WorkflowRun) -> WorkflowRun:
        values = asdict(run)
        values["status"] = run.status.value
        values["state_payload"] = json.dumps(run.state_payload, ensure_ascii=False, sort_keys=True)
        cols = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        self._execute(
            f"INSERT INTO workflow_runs ({cols}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        return run

    def get_run(self, run_id: str) -> WorkflowRun | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["status"] = RunStatus(data["status"])
        data["state_payload"] = json.loads(data["state_payload"] or "{}")
        return WorkflowRun(**data)

    def list_runs(self, project_id: str) -> list[WorkflowRun]:
        with self._lock:
            ids = [
                row["id"]
                for row in self._conn.execute(
                    "SELECT id FROM workflow_runs WHERE project_id=? ORDER BY created_at",
                    (project_id,),
                ).fetchall()
            ]
        return [run for run_id in ids if (run := self.get_run(run_id)) is not None]

    def update_run(
        self,
        run_id: str,
        *,
        status: RunStatus | None = None,
        current_step: str | None = None,
        state_payload: dict[str, Any] | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        updated_at: str,
    ) -> WorkflowRun:
        current = self.get_run(run_id)
        if current is None:
            raise KeyError(f"Unknown run: {run_id}")
        self._execute(
            """
            UPDATE workflow_runs SET status=?,current_step=?,state_payload=?,started_at=?,ended_at=?,updated_at=?
            WHERE id=?
            """,
            (
                (status or current.status).value,
                current_step if current_step is not None else current.current_step,
                json.dumps(
                    state_payload if state_payload is not None else current.state_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                started_at if started_at is not None else current.started_at,
                ended_at if ended_at is not None else current.ended_at,
                updated_at,
                run_id,
            ),
        )
        run = self.get_run(run_id)
        assert run is not None
        return run

    def upsert_run_step(
        self,
        run_id: str,
        step_key: str,
        status: StepStatus,
        *,
        output_ref: str | None,
        error: str | None,
        updated_at: str,
    ) -> None:
        self._execute(
            """
            INSERT INTO workflow_run_steps(run_id,step_key,status,output_ref,error,updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(run_id,step_key) DO UPDATE SET
              status=excluded.status,
              output_ref=excluded.output_ref,
              error=excluded.error,
              updated_at=excluded.updated_at
            """,
            (run_id, step_key, status.value, output_ref, error, updated_at),
        )

    def list_run_steps(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_run_steps WHERE run_id=? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def record_resume_key(self, run_id: str, resume_key: str, created_at: str) -> bool:
        """Return False if the exact resume command was already processed."""
        try:
            self._execute(
                "INSERT INTO workflow_resume_receipts(run_id,resume_key,created_at) VALUES(?,?,?)",
                (run_id, resume_key, created_at),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    # --- Human checkpoints ---
    def create_checkpoint(self, cp: HumanCheckpoint) -> HumanCheckpoint:
        self._execute(
            """
            INSERT INTO checkpoints(id,project_id,step_key,prompt,payload,status,response,created_at,resolved_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                cp.id,
                cp.project_id,
                cp.step_key,
                cp.prompt,
                json.dumps(cp.payload, ensure_ascii=False, sort_keys=True),
                cp.status.value,
                None if cp.response is None else json.dumps(cp.response, ensure_ascii=False, sort_keys=True),
                cp.created_at,
                cp.resolved_at,
            ),
        )
        return cp

    def get_checkpoint(self, checkpoint_id: str) -> HumanCheckpoint | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
        if row is None:
            return None
        return HumanCheckpoint(
            id=row["id"],
            project_id=row["project_id"],
            step_key=row["step_key"],
            prompt=row["prompt"],
            payload=json.loads(row["payload"]),
            status=CheckpointStatus(row["status"]),
            response=json.loads(row["response"]) if row["response"] else None,
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
        )

    def resolve_checkpoint(
        self,
        checkpoint_id: str,
        status: CheckpointStatus,
        response: dict[str, Any],
        resolved_at: str,
    ) -> HumanCheckpoint:
        if status is CheckpointStatus.PENDING:
            raise ValueError("A resolved checkpoint cannot remain pending")
        self._execute(
            "UPDATE checkpoints SET status=?, response=?, resolved_at=? WHERE id=?",
            (status.value, json.dumps(response, ensure_ascii=False, sort_keys=True), resolved_at, checkpoint_id),
        )
        cp = self.get_checkpoint(checkpoint_id)
        if cp is None:
            raise KeyError(f"Unknown checkpoint: {checkpoint_id}")
        return cp
