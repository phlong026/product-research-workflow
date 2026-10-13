from __future__ import annotations

import os
import hmac
import ipaddress
from functools import lru_cache

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool

from research_workflow.store import SQLiteStore
from research_workflow.workflow import WorkflowService


class ProjectCreate(BaseModel):
    product: str = Field(min_length=1)
    marketplace: str = Field(min_length=1)
    primary_keyword: str | None = None
    main_node: str | None = None


class RunCreate(BaseModel):
    graph_version: str = "v1"


class RunResume(BaseModel):
    resume_key: str | None = None


class CheckpointCreate(BaseModel):
    step_key: str
    prompt: str
    payload: dict = Field(default_factory=dict)


class CheckpointResolve(BaseModel):
    approved: StrictBool
    response: dict = Field(default_factory=dict)


@lru_cache(maxsize=1)
def get_service() -> WorkflowService:
    db_path = os.getenv("RESEARCH_DB_PATH", "research_workflow.db")
    return WorkflowService(SQLiteStore(db_path))


def create_app(service: WorkflowService | None = None, *, api_token: str | None = None) -> FastAPI:
    app = FastAPI(title="Product Research Workflow — local state API", version="0.2.0",
                  description="Experimental single-operator state API. Start/resume do not execute research.")
    token = api_token if api_token is not None else os.getenv("RESEARCH_API_TOKEN", "")

    @app.middleware("http")
    async def authorize(request, call_next):
        if not token:
            return JSONResponse({"detail": "Local API disabled: configure RESEARCH_API_TOKEN"}, status_code=503)
        try:
            local = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        if not local:
            return JSONResponse({"detail": "Local connections only"}, status_code=403)
        authorization = request.headers.get("Authorization", "")
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Research-Execution-Mode"] = "state-only"
        return response

    def _service() -> WorkflowService:
        return service or get_service()

    @app.post("/projects")
    def create_project(body: ProjectCreate):
        try:
            svc = _service()
            project = svc.create_project(**body.model_dump())
            return svc.snapshot(project.id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/projects/{project_id}")
    def get_project(project_id: str):
        try:
            return _service().snapshot(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


    @app.post("/projects/{project_id}/runs")
    def create_run(project_id: str, body: RunCreate):
        try:
            svc = _service()
            run = svc.create_run(project_id, body.graph_version)
            svc.start_run(run.id)
            return {**svc.get_run_snapshot(run.id), "execution_mode": "state_only", "executor_started": False}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        try:
            return _service().get_run_snapshot(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/runs/{run_id}/resume")
    def resume_run(run_id: str, body: RunResume):
        try:
            svc = _service()
            svc.resume_run(run_id, resume_key=body.resume_key)
            return svc.get_run_snapshot(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/runs/{run_id}/retry")
    def retry_run(run_id: str):
        try:
            svc = _service()
            retry = svc.retry_run(run_id)
            return svc.get_run_snapshot(retry.id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/projects/{project_id}/start")
    def start_project(project_id: str):
        try:
            svc = _service()
            svc.start(project_id)
            return svc.snapshot(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/projects/{project_id}/checkpoints")
    def create_checkpoint(project_id: str, body: CheckpointCreate):
        try:
            cp = _service().request_human_checkpoint(project_id, **body.model_dump())
            return {"checkpoint_id": cp.id, "status": cp.status.value}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/checkpoints/{checkpoint_id}/resolve")
    def resolve_checkpoint(checkpoint_id: str, body: CheckpointResolve):
        try:
            cp = _service().resolve_human_checkpoint(checkpoint_id, **body.model_dump())
            return {"checkpoint_id": cp.id, "status": cp.status.value, "response": cp.response}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app


app = create_app()
