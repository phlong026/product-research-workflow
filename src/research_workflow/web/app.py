"""多人 Web 研究工作台后端:账号会话、项目隔离、作业提交、工件与资料包下载。

隔离原则:无权访问的项目一律返回 404(不泄露存在性);viewer 只读,editor 可执行作业,
owner 可分享;admin 可访问全部。会话 Cookie 为 HttpOnly + SameSite=Strict,写操作需 CSRF 头。
"""
from __future__ import annotations

import hmac
import io
import re
import zipfile
from hashlib import sha256
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from research_workflow import cli
from research_workflow.store import SQLiteStore
from research_workflow.workflow import WorkflowService

from . import security
from .jobs import JobRunner, JobValidationError
from .store import WebStore

COOKIE = "rw_session"
ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}
UPLOAD_EXTENSIONS = {".csv", ".xlsx", ".json"}
ARTIFACTS = {
    "records.json", "quality.json", "sources.json", "coverage.json", "nodes.json", "enrichment.json",
    "classification.json", "market_metrics.json", "market_analysis.json", "competition.json",
    "new_products.json", "new_product_queue.json", "iteration.json", "route_plan.json", "analysis_context.json",
}
STATIC_DIR = Path(__file__).parent / "static"


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class ProjectBody(BaseModel):
    product: str = Field(min_length=1, max_length=200)
    marketplace: str = Field(pattern=r"^[A-Za-z]{2}$")
    primary_keyword: str | None = Field(default=None, min_length=1, max_length=200)


class JobBody(BaseModel):
    command: str = Field(min_length=1, max_length=40)
    params: dict[str, Any] = Field(default_factory=dict)


class MemberBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    role: str = Field(pattern="^(editor|viewer)$")


class UserBody(BaseModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    password: str = Field(min_length=security.MIN_PASSWORD_LENGTH, max_length=256)
    role: str = Field(pattern="^(admin|researcher)$")


def create_web_app(*, db_path: str | Path, root: str | Path, web_db_path: str | Path,
                   secure_cookies: bool = True, job_workers: int = 2,
                   max_upload_bytes: int = 50 * 1024 * 1024) -> FastAPI:
    db_path, root = str(db_path), Path(root)
    root.mkdir(parents=True, exist_ok=True)
    store = WebStore(web_db_path)
    runner = JobRunner(store, db_path=db_path, root=str(root), workers=job_workers)
    throttle = security.LoginThrottle()
    app = FastAPI(title="Product Research Workbench", version="0.1.0")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # --- helpers ---
    def engine() -> WorkflowService:
        return WorkflowService(SQLiteStore(db_path))

    def session_of(request: Request) -> dict[str, Any]:
        token = request.cookies.get(COOKIE)
        session = store.get_session(token) if token else None
        user = store.get_user(session["user_id"]) if session else None
        if session is None or user is None or user["disabled"]:
            raise HTTPException(status_code=401, detail="未登录或会话已过期")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            sent = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(sent.encode(), session["csrf_token"].encode()):
                raise HTTPException(status_code=403, detail="CSRF 校验失败")
        return {"user": user, "session": session}

    def access(request: Request, project_id: str, need: str = "viewer") -> tuple[dict[str, Any], str]:
        user = session_of(request)["user"]
        role = "owner" if user["role"] == "admin" else store.project_role(project_id, user["id"])
        if role is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        if ROLE_RANK[role] < ROLE_RANK[need]:
            raise HTTPException(status_code=403, detail="权限不足")
        return user, role

    def upload_path(project_id: str):
        def lookup(upload_id: str) -> Path:
            row = store.get_upload(project_id, upload_id)
            if row is None:
                raise JobValidationError(f"上传文件不存在: {upload_id}")
            return Path(row["path"])
        return lookup

    # --- auth ---
    @app.post("/api/auth/login")
    def login(body: LoginBody, response: Response):
        if throttle.locked(body.username):
            raise HTTPException(status_code=429, detail="登录失败次数过多,请稍后再试")
        user = store.get_user_by_name(body.username)
        if user is None or user["disabled"]:
            security.burn_password_check(body.password)
            throttle.record_failure(body.username)
            raise HTTPException(status_code=401, detail="用户名或口令错误")
        if not security.verify_password(body.password, user["password_hash"]):
            throttle.record_failure(body.username)
            raise HTTPException(status_code=401, detail="用户名或口令错误")
        throttle.reset(body.username)
        token, csrf = store.create_session(user["id"])
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=secure_cookies,
                            max_age=security.SESSION_TTL_SECONDS, path="/")
        return {"username": user["username"], "role": user["role"], "csrf_token": csrf}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        ctx = session_of(request)
        store.delete_session(request.cookies[COOKIE])
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True, "user": ctx["user"]["username"]}

    @app.get("/api/me")
    def me(request: Request):
        ctx = session_of(request)
        return {"username": ctx["user"]["username"], "role": ctx["user"]["role"], "csrf_token": ctx["session"]["csrf_token"]}

    @app.post("/api/admin/users", status_code=201)
    def create_user(body: UserBody, request: Request):
        if session_of(request)["user"]["role"] != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可创建账号")
        try:
            store.create_user(body.username, body.password, body.role)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"username": body.username, "role": body.role}

    # --- projects ---
    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectBody, request: Request):
        user = session_of(request)["user"]
        argv = ["init", f"--product={body.product}", f"--marketplace={body.marketplace.upper()}"]
        if body.primary_keyword:
            argv.append(f"--primary-keyword={body.primary_keyword}")
        try:
            out = runner.run_sync(argv)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.grant(out["project_id"], user["id"], "owner")
        return {"project_id": out["project_id"]}

    @app.get("/api/projects")
    def list_projects(request: Request):
        user = session_of(request)["user"]
        svc = engine()
        try:
            if user["role"] == "admin":
                entries = [(p.id, "owner") for p in svc.store.list_projects()]
            else:
                entries = [(e["project_id"], e["role"]) for e in store.project_ids_for(user["id"])]
            projects = []
            for pid, role in entries:
                project = svc.store.get_project(pid)
                if project is not None:
                    projects.append({"project_id": pid, "product": project.product, "marketplace": project.marketplace,
                                     "status": project.status.value, "role": role, "created_at": project.created_at})
            return {"projects": sorted(projects, key=lambda p: p["created_at"], reverse=True)}
        finally:
            svc.store.close()

    @app.get("/api/projects/{project_id}")
    def project_detail(project_id: str, request: Request):
        _, role = access(request, project_id)
        svc = engine()
        try:
            payload = cli.status_payload(svc, root, project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        finally:
            svc.store.close()
        return {**payload, "role": role}

    @app.post("/api/projects/{project_id}/members")
    def add_member(project_id: str, body: MemberBody, request: Request):
        access(request, project_id, "owner")
        target = store.get_user_by_name(body.username)
        if target is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if store.project_role(project_id, target["id"]) == "owner":
            raise HTTPException(status_code=409, detail="不能修改所有者角色")
        store.grant(project_id, target["id"], body.role)
        return {"username": body.username, "role": body.role}

    # --- uploads / artifacts / dossiers ---
    @app.put("/api/projects/{project_id}/uploads", status_code=201)
    async def upload(project_id: str, filename: str, request: Request):
        user, _ = access(request, project_id, "editor")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename.replace("\\", "/")).name).lstrip(".") or "upload"
        if Path(safe).suffix.lower() not in UPLOAD_EXTENSIONS:
            raise HTTPException(status_code=415, detail="仅支持 csv / xlsx / json 文件")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_upload_bytes:
            raise HTTPException(status_code=413, detail="文件过大")
        data = await request.body()
        if len(data) > max_upload_bytes:
            raise HTTPException(status_code=413, detail="文件过大")
        digest = sha256(data).hexdigest()
        directory = root / project_id / "uploads"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{digest[:16]}__{safe}"
        target.write_bytes(data)  # 先落盘再登记,避免出现指向不存在文件的记录
        upload_id = store.add_upload(project_id=project_id, filename=safe, sha256=digest, size=len(data),
                                     path=str(target), created_by=user["id"])
        return {"upload_id": upload_id, "filename": safe, "sha256": digest, "size": len(data)}

    @app.get("/api/projects/{project_id}/artifacts/{name}")
    def artifact(project_id: str, name: str, request: Request):
        access(request, project_id)
        path = root / project_id / name
        if name not in ARTIFACTS or not path.is_file():
            raise HTTPException(status_code=404, detail="工件不存在")
        return FileResponse(path, media_type="application/json")

    def dossier_dirs(project_id: str) -> list[Path]:
        base = root / project_id / "dossiers"
        found = [p for p in base.iterdir() if re.fullmatch(r"v\d+", p.name) and (p / "manifest.json").is_file()] if base.is_dir() else []
        return sorted(found, key=lambda p: int(p.name[1:]))

    @app.get("/api/projects/{project_id}/dossiers")
    def dossiers(project_id: str, request: Request):
        access(request, project_id)
        return {"dossiers": [{"version": p.name, "files": sum(1 for f in p.rglob("*") if f.is_file())}
                             for p in dossier_dirs(project_id)]}

    @app.get("/api/projects/{project_id}/dossiers/{version}/download.zip")
    def dossier_zip(project_id: str, version: str, request: Request):
        access(request, project_id)
        target = next((p for p in dossier_dirs(project_id) if p.name == version), None)
        if target is None:
            raise HTTPException(status_code=404, detail="资料包不存在")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(target.rglob("*")):
                if file.is_file():
                    archive.write(file, file.relative_to(target).as_posix())
        return Response(buffer.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{project_id}-{version}.zip"'})

    # --- jobs ---
    def submit(project_id: str, command: str, params: dict[str, Any], user: dict[str, Any],
               attempts: int = 1, retry_of: str | None = None):
        try:
            job_id = runner.submit(project_id=project_id, command=command, params=params, user_id=user["id"],
                                   upload_path=upload_path(project_id), attempts=attempts, retry_of=retry_of)
        except JobValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if job_id is None:
            raise HTTPException(status_code=409, detail="该项目已有进行中的作业,请等待完成后再提交")
        return JSONResponse({"job_id": job_id}, status_code=202)

    @app.post("/api/projects/{project_id}/jobs")
    def create_job(project_id: str, body: JobBody, request: Request):
        user, _ = access(request, project_id, "editor")
        return submit(project_id, body.command, body.params, user)

    @app.get("/api/projects/{project_id}/jobs")
    def list_jobs(project_id: str, request: Request):
        access(request, project_id)
        return {"jobs": store.list_jobs(project_id)}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="作业不存在")
        access(request, job["project_id"])
        return job

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str, request: Request):
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="作业不存在")
        user, _ = access(request, job["project_id"], "editor")
        if job["status"] != "failed":
            raise HTTPException(status_code=409, detail="只有失败的作业可以重试")
        return submit(job["project_id"], job["command"], job["params"], user,
                      attempts=job["attempts"] + 1, retry_of=job_id)

    @app.get("/", include_in_schema=False)
    def index():
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            raise HTTPException(status_code=404, detail="前端尚未构建")
        return FileResponse(page)

    if STATIC_DIR.is_dir():  # CSP 禁止内联脚本:前端的 js/css 以独立文件由此提供
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.state.runner = runner
    app.state.store = store
    return app
