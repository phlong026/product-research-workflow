from __future__ import annotations

import os
from functools import lru_cache

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

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
    approved: bool
    response: dict = Field(default_factory=dict)


@lru_cache(maxsize=1)
def get_service() -> WorkflowService:
    db_path = os.getenv("RESEARCH_DB_PATH", "research_workflow.db")
    return WorkflowService(SQLiteStore(db_path))


def create_app(service: WorkflowService | None = None) -> FastAPI:
    app = FastAPI(title="Product Research Workflow", version="0.2.0")
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


    @app.post("/projects/{project_id}/runs")
    def create_run(project_id: str, body: RunCreate):
        try:
            svc = _service()
            run = svc.create_run(project_id, body.graph_version)
            svc.start_run(run.id)
            return svc.get_run_snapshot(run.id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        try:
            return _service().get_run_snapshot(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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

    @app.post("/projects/{project_id}/checkpoints")
    def create_checkpoint(project_id: str, body: CheckpointCreate):
        try:
            cp = _service().request_human_checkpoint(project_id, **body.model_dump())
            return {"checkpoint_id": cp.id, "status": cp.status.value}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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
