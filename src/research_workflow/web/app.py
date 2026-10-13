"""多人使用的研究工作台（Web）。

- 具名用户 + 令牌（只存哈希）；项目成员角色 owner/editor/viewer；admin 全局管理
- 所有研究动作都是“命令白名单 + 结构化参数”提交的后台任务，持久化在 SQLite 队列
- 引擎状态（审批、哈希绑定、版本）仍只由 run_research.py CLI 写入，工作台不绕过任何人工检查点
- 上传文件按项目隔离，任务只能引用本项目已上传文件的 ID
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .commands import RESOLVE_ARGS, RESOLVE_SPEC, SPECS, build_argv
from .db import WorkbenchStore
from .runner import JobWorker, run_cli

REPO_ROOT = Path(__file__).resolve().parents[3]
STATIC = Path(__file__).parent / "static"
PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SAFE_NAME = re.compile(r"[^A-Za-z0-9._\-一-鿿]+")
ALLOWED_UPLOAD_SUFFIXES = {".csv", ".xlsx", ".xlsm", ".json", ".pdf", ".txt", ".png", ".jpg", ".jpeg"}


class ProjectCreate(BaseModel):
    product: str = Field(min_length=1, max_length=200)
    marketplace: str = Field(min_length=1, max_length=16)
    primary_keyword: str | None = Field(default=None, max_length=200)
    main_node: str | None = Field(default=None, max_length=200)


class JobCreate(BaseModel):
    command: str
    params: dict[str, Any] = Field(default_factory=dict)


class ResolveBody(BaseModel):
    checkpoint: str
    approve: bool
    edits: list[str] = Field(default_factory=list)   # "ASIN=标签"
    select: str | None = None                        # 路线 id，逗号分隔
    edits_file: str | None = None                    # 上传的表3 ID


class UserCreate(BaseModel):
    name: str
    role: str = "member"


class MemberBody(BaseModel):
    user: str
    role: str


def create_app(
    *, workbench_db: str | None = None, engine_db: str | None = None, root: str | None = None,
    max_upload_mb: int = 50, allow_live: bool | None = None, start_worker: bool = True,
    script: Path | None = None,
) -> FastAPI:
    workbench_db = workbench_db or os.environ.get("WORKBENCH_DB", str(REPO_ROOT / "runtime" / "workbench.db"))
    engine_db = engine_db or os.environ.get("RESEARCH_DB_PATH", str(REPO_ROOT / "runtime" / "research.db"))
    root = root or os.environ.get("RESEARCH_ROOT", str(REPO_ROOT / "runtime" / "projects"))
    allow_live = allow_live if allow_live is not None else os.environ.get("WORKBENCH_ALLOW_LIVE") == "1"
    script = script or REPO_ROOT / "scripts" / "run_research.py"
    store = WorkbenchStore(workbench_db)
    worker = JobWorker(store, script=script, engine_db=engine_db, root=root)
    max_bytes = max_upload_mb * 1024 * 1024

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_worker:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(title="Product Research Workbench", version="1.0.0", lifespan=lifespan)
    app.state.store, app.state.worker = store, worker

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
        response.headers["Cache-Control"] = "no-store"
        return response

    def current_user(request: Request) -> dict[str, Any]:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(401, "需要 Bearer 令牌")
        user = store.user_by_token(header[7:].strip())
        if user is None:
            raise HTTPException(401, "令牌无效或用户已停用")
        return user

    def require_admin(user: dict = Depends(current_user)) -> dict[str, Any]:
        if user["role"] != "admin":
            raise HTTPException(403, "需要管理员权限")
        return user

    def project_access(project_id: str, user: dict[str, Any], needed: str) -> str:
        if not PROJECT_ID.match(project_id):
            raise HTTPException(404, "项目不存在")
        role = store.member_role(project_id, user)
        if role is None:
            raise HTTPException(404, "项目不存在")  # 对非成员不暴露项目是否存在
        if not store.at_least(role, needed):
            raise HTTPException(403, f"需要项目 {needed} 权限")
        return role

    def pdir(project_id: str) -> Path:
        d = Path(root) / project_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def cli(command: str, argv: list[str], timeout: int = 120) -> Any:
        code, payload, error = run_cli(worker.python, script, engine_db, root, command, argv, timeout=timeout)
        if error is not None:
            raise HTTPException(400, error)
        return payload

    def resolver(project_id: str):
        def resolve_file(upload_id: str) -> str:
            up = store.get_upload(project_id, upload_id)
            if up is None:
                raise ValueError(f"文件不存在或不属于本项目: {upload_id}")
            return up["path"]
        return resolve_file

    # ---------- 页面 ----------
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ---------- 用户 ----------
    @app.get("/api/me")
    def me(user: dict = Depends(current_user)):
        return user

    @app.post("/api/users")
    def create_user(body: UserCreate, admin: dict = Depends(require_admin)):
        try:
            user, token = store.create_user(body.name, body.role)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # 重名
            raise HTTPException(409, "用户已存在") from exc
        store.audit(admin["name"], "user_created", None, user=user["name"], role=user["role"])
        return {**user, "token": token, "note": "令牌只显示这一次，请安全转交"}

    @app.get("/api/users")
    def list_users(_: dict = Depends(require_admin)):
        return store.list_users()

    # ---------- 项目 ----------
    @app.get("/api/projects")
    def list_projects(user: dict = Depends(current_user)):
        allowed = store.projects_of(user)
        items = cli("projects", [])["projects"]
        return [p for p in items if allowed is None or p["project_id"] in allowed]

    @app.post("/api/projects")
    def create_project(body: ProjectCreate, user: dict = Depends(current_user)):
        argv = [f"--product={body.product}", f"--marketplace={body.marketplace}"]
        if body.primary_keyword:
            argv.append(f"--primary-keyword={body.primary_keyword}")
        if body.main_node:
            argv.append(f"--main-node={body.main_node}")
        out = cli("init", argv)
        store.add_member(out["project_id"], user["id"], "owner")
        store.audit(user["name"], "project_created", out["project_id"], product=body.product)
        return out

    @app.get("/api/projects/{project_id}/status")
    def project_status(project_id: str, user: dict = Depends(current_user)):
        project_access(project_id, user, "viewer")
        return cli("status", [f"--project={project_id}"])

    @app.get("/api/projects/{project_id}/members")
    def get_members(project_id: str, user: dict = Depends(current_user)):
        project_access(project_id, user, "viewer")
        return store.members(project_id)

    @app.put("/api/projects/{project_id}/members")
    def put_member(project_id: str, body: MemberBody, user: dict = Depends(current_user)):
        project_access(project_id, user, "owner")
        target = store.user_by_name(body.user)
        if target is None or target["disabled"]:
            raise HTTPException(404, "用户不存在或已停用")
        try:
            store.add_member(project_id, target["id"], body.role)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        store.audit(user["name"], "member_set", project_id, member=body.user, role=body.role)
        return store.members(project_id)

    @app.delete("/api/projects/{project_id}/members/{name}")
    def delete_member(project_id: str, name: str, user: dict = Depends(current_user)):
        project_access(project_id, user, "owner")
        target = store.user_by_name(name)
        if target is None:
            raise HTTPException(404, "用户不存在")
        owners = [m for m in store.members(project_id) if m["role"] == "owner"]
        if len(owners) == 1 and owners[0]["id"] == target["id"]:
            raise HTTPException(409, "不能移除唯一的项目负责人")
        store.remove_member(project_id, target["id"])
        store.audit(user["name"], "member_removed", project_id, member=name)
        return store.members(project_id)

    # ---------- 上传 ----------
    @app.put("/api/projects/{project_id}/uploads")
    async def upload(project_id: str, request: Request, filename: str = Query(min_length=1, max_length=200),
                     user: dict = Depends(current_user)):
        project_access(project_id, user, "editor")
        safe = SAFE_NAME.sub("_", Path(filename).name).strip("._") or "upload"
        if Path(safe).suffix.lower() not in ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(400, f"不支持的文件类型，允许: {sorted(ALLOWED_UPLOAD_SUFFIXES)}")
        upload_id = uuid.uuid4().hex
        dest_dir = pdir(project_id) / "uploads" / upload_id
        dest_dir.mkdir(parents=True)
        dest = dest_dir / safe
        digest, size = hashlib.sha256(), 0
        try:
            with dest.open("wb") as handle:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(413, f"文件超过 {max_upload_mb}MB 上限")
                    digest.update(chunk)
                    handle.write(chunk)
            if size == 0:
                raise HTTPException(400, "空文件")
        except BaseException:
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise
        store.add_upload(project_id=project_id, filename=safe, path=str(dest), size=size,
                         sha256=digest.hexdigest(), user=user["name"], upload_id=upload_id)
        store.audit(user["name"], "upload", project_id, upload_id=upload_id, filename=safe, size=size,
                    sha256=digest.hexdigest())
        return {"upload_id": upload_id, "filename": safe, "size": size, "sha256": digest.hexdigest()}

    @app.get("/api/projects/{project_id}/uploads")
    def list_uploads(project_id: str, user: dict = Depends(current_user)):
        project_access(project_id, user, "viewer")
        return store.list_uploads(project_id)

    # ---------- 任务 ----------
    def enqueue(project_id: str, user: dict, command: str, params: dict[str, Any]) -> dict[str, Any]:
        spec = SPECS.get(command)
        if spec is None:
            raise HTTPException(400, f"不支持的命令: {command}；可用: {sorted(SPECS)}")
        project_access(project_id, user, spec.needs)
        job_id = uuid.uuid4().hex
        d = pdir(project_id)
        output = None
        if spec.server_output:
            if command == "dossier":
                output = str(d / "dossiers" / job_id)
            else:
                (d / "exports").mkdir(exist_ok=True)
                fmt = params.get("format", "xlsx")
                tag = f"table{params.get('table')}" if command == "table-export" else "new_products"
                output = str(d / "exports" / f"{tag}-{job_id[:8]}.{'csv' if command == 'new-products-select' else fmt}")
        forced = {"added_by": user["name"]} if command == "evidence-add" else None
        try:
            argv = build_argv(spec, params, resolve_file=resolver(project_id), output_path=output,
                              allow_live=allow_live, forced=forced)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        argv = [f"--project={project_id}", *argv]
        store.enqueue(project_id=project_id, command=command, user=user["name"], job_id=job_id,
                      args={"params": params, "argv": argv, "timeout": spec.timeout, "output": output})
        store.audit(user["name"], "job_submitted", project_id, job_id=job_id, command=command, params=params)
        worker.notify()
        return {"job_id": job_id, "status": "queued"}

    @app.post("/api/projects/{project_id}/jobs")
    def submit_job(project_id: str, body: JobCreate, user: dict = Depends(current_user)):
        return enqueue(project_id, user, body.command, body.params)

    @app.post("/api/projects/{project_id}/resolve")
    def resolve(project_id: str, body: ResolveBody, user: dict = Depends(current_user)):
        """人工检查点裁决：必须是具名项目 editor/owner，并写入审计（谁在何时批准了什么）。"""
        project_access(project_id, user, "editor")
        params: dict[str, Any] = {"checkpoint": body.checkpoint, "approve": body.approve, "reject": not body.approve,
                                  "edit": body.edits, "select": body.select, "edits_file": body.edits_file}
        try:
            argv = build_argv(RESOLVE_SPEC, params, resolve_file=resolver(project_id))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        job_id = uuid.uuid4().hex
        store.enqueue(project_id=project_id, command="resolve", user=user["name"], job_id=job_id,
                      args={"params": params, "argv": [f"--project={project_id}", *argv], "timeout": 600})
        store.audit(user["name"], "checkpoint_resolution_submitted", project_id, job_id=job_id,
                    checkpoint=body.checkpoint, approve=body.approve, edits=body.edits, select=body.select)
        worker.notify()
        return {"job_id": job_id, "status": "queued"}

    @app.get("/api/projects/{project_id}/jobs")
    def list_jobs(project_id: str, user: dict = Depends(current_user)):
        project_access(project_id, user, "viewer")
        return store.list_jobs(project_id)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, user: dict = Depends(current_user)):
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        project_access(job["project_id"], user, "viewer")
        return job

    # ---------- 下载 ----------
    @app.get("/api/projects/{project_id}/exports")
    def list_exports(project_id: str, user: dict = Depends(current_user)):
        project_access(project_id, user, "viewer")
        d = pdir(project_id)
        files = sorted(p.name for p in (d / "exports").glob("*") if p.is_file()) if (d / "exports").exists() else []
        dossiers = sorted(p.name for p in (d / "dossiers").glob("*") if p.is_dir()) if (d / "dossiers").exists() else []
        return {"exports": files, "dossier_job_ids": dossiers}

    @app.get("/api/projects/{project_id}/exports/{name}")
    def download_export(project_id: str, name: str, user: dict = Depends(current_user)):
        project_access(project_id, user, "viewer")
        if name != Path(name).name or name.startswith("."):
            raise HTTPException(400, "非法文件名")
        path = pdir(project_id) / "exports" / name
        if not path.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(path, filename=name)

    @app.get("/api/projects/{project_id}/dossiers/{job_id}.zip")
    def download_dossier(project_id: str, job_id: str, user: dict = Depends(current_user)):
        project_access(project_id, user, "viewer")
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise HTTPException(400, "非法任务 ID")
        folder = pdir(project_id) / "dossiers" / job_id
        if not folder.is_dir():
            raise HTTPException(404, "资料包不存在")
        tmp = Path(tempfile.mkstemp(suffix=".zip")[1])
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(folder.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    zf.write(path, path.relative_to(folder).as_posix())
        from starlette.background import BackgroundTask
        return FileResponse(tmp, filename=f"dossier-{job_id[:8]}.zip", background=BackgroundTask(tmp.unlink, missing_ok=True))

    # ---------- 审计 ----------
    @app.get("/api/projects/{project_id}/audit")
    def audit(project_id: str, user: dict = Depends(current_user)):
        project_access(project_id, user, "owner")
        return store.audit_log(project_id)

    @app.exception_handler(KeyError)
    async def key_error(_, exc: KeyError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    return app


def create_app_from_env() -> FastAPI:
    return create_app()
