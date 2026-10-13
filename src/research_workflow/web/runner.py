from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .db import WorkbenchStore


def run_cli(
    python: str, script: Path, engine_db: str, root: str, command: str, argv: list[str], *, timeout: int
) -> tuple[int | None, Any, str | None]:
    """以子进程执行 run_research.py 一个子命令（列表传参，无 shell）。返回 (returncode, 结果JSON, 错误文本)。"""
    cmd = [python, str(script), "--db", engine_db, "--root", root, command, *argv]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(script.parent.parent))
    except subprocess.TimeoutExpired:
        return None, None, f"命令超时（>{timeout}s）已终止；引擎事务已回滚，请检查后重试"
    payload: Any = None
    out = proc.stdout.strip()
    for candidate in ([out, out.splitlines()[-1]] if out else []):
        try:
            payload = json.loads(candidate)
            break
        except ValueError:
            continue
    if out and payload is None:
        payload = {"raw_output": out[-4000:]}
    if proc.returncode != 0:
        error = (payload or {}).get("error") if isinstance(payload, dict) else None
        return proc.returncode, payload, error or (proc.stderr.strip()[-2000:] or f"退出码 {proc.returncode}")
    return proc.returncode, payload, None


class JobWorker:
    """后台任务执行器：单线程串行消费 SQLite 任务队列，重启后队列仍在。"""

    def __init__(self, store: WorkbenchStore, *, script: Path, engine_db: str, root: str,
                 python: str | None = None, poll_seconds: float = 0.5) -> None:
        self.store, self.script, self.engine_db, self.root = store, script, engine_db, root
        self.python = python or sys.executable
        self.poll = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()

    def start(self) -> None:
        self.store.interrupt_running()
        self._thread = threading.Thread(target=self._loop, name="job-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def notify(self) -> None:
        self._wake.set()

    def run_pending(self) -> int:
        """同步消费队列（测试与单次执行用）。"""
        n = 0
        while (job := self.store.claim_next()) is not None:
            self._execute(job)
            n += 1
        return n

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self.store.claim_next()
            if job is None:
                self._wake.wait(self.poll)
                self._wake.clear()
                continue
            self._execute(job)

    def _execute(self, job: dict) -> None:
        args = json.loads(job["args_json"])
        try:
            code, payload, error = run_cli(self.python, self.script, self.engine_db, self.root,
                                           job["command"], args["argv"], timeout=args.get("timeout", 3600))
        except Exception as exc:  # 任何执行器异常都必须落库，不能让任务永远 running
            code, payload, error = None, None, f"执行器异常: {exc}"
        self.store.finish(job["id"], returncode=code, result=payload, error=error)
        self.store.audit("system", "job_finished", job["project_id"], job_id=job["id"], command=job["command"],
                         ok=error is None, requested_by=job["created_by"])
