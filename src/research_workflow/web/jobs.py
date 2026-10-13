"""研究作业:参数校验 → 引擎 CLI 参数 → 子进程执行(T4-1)。

用户输入只会成为 argv 列表中的单个元素(`--flag=value` 形式),从不经过 shell;
引擎每个阶段的结果与检查点都已落盘,所以"重试/断点恢复"就是重跑对应命令。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_workflow.domain.models import utc_now_iso

from .store import WebStore

logger = logging.getLogger("research_workflow.web.jobs")

MONTH = r"^\d{4}-\d{2}$"
KEY = r"^[A-Za-z0-9_.:-]{1,64}$"
ASIN = r"^[A-Za-z0-9]{1,20}$"
COMMAND_TIMEOUTS = {"ingest": 3600, "discover-nodes": 900, "classify": 1800, "analyze": 900,
                    "resolve": 300, "dossier": 600, "export-table": 120, "import-table": 300,
                    "import-evidence": 300, "build-market-forecast": 300, "build-cross-channel": 300}


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IngestParams(_Params):
    uploads: list[str] = Field(default_factory=list, max_length=60)
    month: str | None = Field(default=None, pattern=MONTH)
    top_n: int = Field(default=100, ge=1, le=1000)
    live: bool = False
    keyword: str | None = Field(default=None, min_length=1, max_length=200)
    months: str | None = Field(default=None, pattern=r"^(auto|\d{4}-\d{2}(,\d{4}-\d{2}){0,59})$")
    nodes: list[str] = Field(default_factory=list, max_length=20)
    candidate_n: int = Field(default=200, ge=1, le=2000)
    page_size: int = Field(default=100, ge=1, le=100)
    max_pages: int = Field(default=20, ge=1, le=20)


class DiscoverParams(_Params):
    keyword: str | None = Field(default=None, min_length=1, max_length=200)
    month: str | None = Field(default=None, pattern=MONTH)


class ClassifyParams(_Params):
    labels: dict[str, str] = Field(min_length=1, max_length=50)
    rules: dict[str, str] = Field(default_factory=dict, max_length=50)
    dimension: str = Field(default="segment", min_length=1, max_length=64)
    provider: str = Field(default="rules", pattern="^(rules|llm)$")
    enrich: bool = False


class ResolveParams(_Params):
    checkpoint_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    approve: bool
    edits: dict[str, str] = Field(default_factory=dict, max_length=2000)
    select: list[str] = Field(default_factory=list, max_length=50)


class AnalyzeParams(_Params):
    new_products_upload: str | None = None
    sales_threshold: int = Field(default=250, ge=0)
    consecutive_months: int = Field(default=3, ge=1, le=24)
    early_window: int = Field(default=3, ge=1, le=24)
    survival_threshold: int | None = Field(default=None, ge=0)
    promotion_threshold: int | None = Field(default=None, ge=0)
    retention_threshold: int | None = Field(default=None, ge=0)
    study_month: str | None = Field(default=None, pattern=MONTH)
    pairs: list[str] = Field(default_factory=list, max_length=20)
    periods: list[str] = Field(default_factory=list, max_length=20)


class DossierParams(_Params):
    pass


EVIDENCE_KINDS = "^(trends|report-excerpts|tiktok-units|patents|patent-search-log|ads|big-brand|offsite|cross-category)$"


class ImportEvidenceParams(_Params):
    kind: str = Field(pattern=EVIDENCE_KINDS)
    upload: str = Field(min_length=1, max_length=64)
    meta: dict[str, str] = Field(default_factory=dict, max_length=20)


class MarketForecastParams(_Params):
    horizon_years: int = Field(ge=1, le=10)
    scope: str = Field(default="top_sample", pattern="^top_sample$")
    method: str = Field(default="cagr", pattern="^(cagr|linear)$")
    granularity: str = Field(default="monthly", pattern="^(monthly|annual)$")
    target_scope: str | None = Field(default=None, pattern="^(amazon_channel|full_market)$")
    coverage_ratio: float | None = Field(default=None, gt=0, le=1)
    coverage_source: str | None = Field(default=None, min_length=1, max_length=300)
    low_growth: float | None = Field(default=None, gt=-1, lt=10)
    high_growth: float | None = Field(default=None, gt=-1, lt=10)


class CrossChannelParams(_Params):
    geo: str | None = Field(default=None, pattern="^(global|us)$")
    segment: str | None = Field(default=None, pattern="^(total|countertop)$")


class ExportTableParams(_Params):
    table: int = Field(ge=1, le=3)


class ImportTableParams(_Params):
    table: int = Field(ge=1, le=3)
    upload: str = Field(min_length=1, max_length=64)


PARAM_MODELS: dict[str, type[_Params]] = {
    "ingest": IngestParams, "discover-nodes": DiscoverParams, "classify": ClassifyParams,
    "resolve": ResolveParams, "analyze": AnalyzeParams, "dossier": DossierParams,
    "export-table": ExportTableParams, "import-table": ImportTableParams,
    "import-evidence": ImportEvidenceParams, "build-market-forecast": MarketForecastParams,
    "build-cross-channel": CrossChannelParams,
}


class JobValidationError(ValueError):
    pass


def validate_params(command: str, params: dict[str, Any]) -> _Params:
    model = PARAM_MODELS.get(command)
    if model is None:
        raise JobValidationError(f"不支持的命令: {command}")
    try:
        parsed = model(**params)
    except ValidationError as exc:
        raise JobValidationError(exc.errors()[0]["msg"] + ": " + ".".join(str(p) for p in exc.errors()[0]["loc"])) from exc
    _check_key_value_params(parsed)
    return parsed


def _check_key_value_params(parsed: _Params) -> None:
    for name in ("labels", "rules", "edits"):
        mapping = getattr(parsed, name, None) or {}
        for key, value in mapping.items():
            pattern = ASIN if name == "edits" else KEY
            if not re.fullmatch(pattern, key):
                raise JobValidationError(f"{name} 键不合法: {key!r}")
            if not isinstance(value, str) or not value.strip() or len(value) > 500 or "\n" in value:
                raise JobValidationError(f"{name}[{key}] 值不合法")
    for key, value in (getattr(parsed, "meta", None) or {}).items():
        if not re.fullmatch(r"^[a-z_]{1,32}$", key):
            raise JobValidationError(f"meta 键不合法: {key!r}")
        if not value.strip() or len(value) > 500 or "\n" in value or "\r" in value:
            raise JobValidationError(f"meta[{key}] 值不合法")
    for route in getattr(parsed, "select", []) or []:
        if not re.fullmatch(r"^[A-Za-z0-9_:.-]{1,80}$", route):
            raise JobValidationError(f"路线 ID 不合法: {route!r}")
    for node in getattr(parsed, "nodes", []) or []:
        if not re.fullmatch(r"^[A-Za-z0-9:_,.\-/ ]{1,200}$", node) or "," in node:
            raise JobValidationError(f"节点不合法: {node!r}")
    for item in [*(getattr(parsed, "pairs", []) or []), *(getattr(parsed, "periods", []) or [])]:
        if not re.fullmatch(r"^[0-9:\-]{7,40}$", item):
            raise JobValidationError(f"比较区间不合法: {item!r}")


def next_dossier_dir(project_dir: Path) -> Path:
    base = project_dir / "dossiers"
    base.mkdir(parents=True, exist_ok=True)
    existing = [int(m.group(1)) for p in base.iterdir() if (m := re.fullmatch(r"v(\d+)", p.name))]
    return base / f"v{max(existing, default=0) + 1}"


def build_argv(command: str, params: _Params, *, project_id: str, project_dir: Path,
               upload_path: Callable[[str], Path]) -> list[str]:
    """把已校验参数转成引擎 CLI 参数。所有值以 `--flag=value` 传入。"""
    p = params.model_dump()
    argv = [command, f"--project={project_id}"]
    if command == "ingest":
        if p["live"]:
            if not p["keyword"] or not p["months"]:
                raise JobValidationError("live 取数必须提供 keyword 与 months")
            argv += ["--live", f"--keyword={p['keyword']}", f"--months={p['months']}",
                     f"--candidate-n={p['candidate_n']}", f"--page-size={p['page_size']}",
                     f"--max-pages={p['max_pages']}"]
            if p["nodes"]:
                argv.append("--nodes=" + ",".join(p["nodes"]))
        else:
            if not p["uploads"]:
                raise JobValidationError("文件导入必须至少选择一个已上传文件")
            argv += [f"--file={upload_path(u)}" for u in p["uploads"]]
            if p["month"]:
                argv.append(f"--month={p['month']}")
        argv.append(f"--top-n={p['top_n']}")
    elif command == "discover-nodes":
        argv += [f"--{k}={v}" for k, v in (("keyword", p["keyword"]), ("month", p["month"])) if v]
    elif command == "classify":
        argv += [f"--label={k}={v}" for k, v in p["labels"].items()]
        argv += [f"--rule={k}={v}" for k, v in p["rules"].items()]
        argv += [f"--dimension={p['dimension']}", f"--provider={p['provider']}"]
        if p["enrich"]:
            argv.append("--enrich")
    elif command == "resolve":
        argv += [f"--checkpoint={p['checkpoint_id']}", "--approve" if p["approve"] else "--reject"]
        argv += [f"--edit={k}={v}" for k, v in p["edits"].items()]
        if p["select"]:
            argv.append("--select=" + ",".join(p["select"]))
    elif command == "analyze":
        if p["new_products_upload"]:
            argv.append(f"--new-products-file={upload_path(p['new_products_upload'])}")
        for flag, key in (("sales-threshold", "sales_threshold"), ("consecutive-months", "consecutive_months"),
                          ("early-window", "early_window"), ("survival-threshold", "survival_threshold"),
                          ("promotion-threshold", "promotion_threshold"),
                          ("retention-threshold", "retention_threshold"), ("study-month", "study_month")):
            if p[key] is not None:
                argv.append(f"--{flag}={p[key]}")
        argv += [f"--pair={x}" for x in p["pairs"]] + [f"--period={x}" for x in p["periods"]]
    elif command == "dossier":
        argv.append(f"--output={next_dossier_dir(project_dir)}")
    elif command == "import-evidence":
        argv += [f"--kind={p['kind']}", f"--file={upload_path(p['upload'])}"]
        argv += [f"--meta={k}={v}" for k, v in p["meta"].items()]
    elif command == "build-market-forecast":
        argv += [f"--horizon-years={p['horizon_years']}"]
        for flag, key in (("scope", "scope"), ("method", "method"), ("granularity", "granularity"),
                          ("target-scope", "target_scope"), ("coverage-ratio", "coverage_ratio"),
                          ("coverage-source", "coverage_source"), ("low-growth", "low_growth"),
                          ("high-growth", "high_growth")):
            if p[key] is not None:
                argv.append(f"--{flag}={p[key]}")
    elif command == "build-cross-channel":
        argv += [f"--{k}={p[k]}" for k in ("geo", "segment") if p[k]]
    elif command == "export-table":
        exports = project_dir / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        argv += [f"--table={p['table']}", f"--output={exports / ('table' + str(p['table']) + '.csv')}"]
    elif command == "import-table":
        argv += [f"--table={p['table']}", f"--file={upload_path(p['upload'])}"]
    return argv


# 子进程只继承引擎确实需要的环境变量,不把宿主机上无关的密钥(云凭据等)带进去。
ENV_ALLOW_EXACT = {"PATH", "HOME", "USER", "LANG", "TMPDIR", "TZ", "PYTHONPATH", "PYTHONUTF8", "VIRTUAL_ENV",
                   "PRODUCT_RESEARCH_ENGINE", "ANTHROPIC_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                   "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE"}
ENV_ALLOW_PREFIXES = ("LC_", "SELLERSPRITE_", "SIF_", "XIYOU_", "APIFY_", "RESEARCH_LLM_")
# 回传给前端的结果里去掉服务器绝对路径
PRIVATE_RESULT_KEYS = {"artifact_dir", "records_file", "dossier_dir", "report_md", "dashboard_html", "file"}
GENERIC_ENGINE_ERROR = "引擎内部错误,请联系管理员并提供参考号 "


def engine_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if k in ENV_ALLOW_EXACT or k.startswith(ENV_ALLOW_PREFIXES)}


def public_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (payload or {}).items() if k not in PRIVATE_RESULT_KEYS}


def run_engine(argv: list[str], *, db_path: str, root: str, timeout: int) -> tuple[int, dict[str, Any] | None, str]:
    """在子进程中运行引擎 CLI;返回 (退出码, 末行 JSON, 截断后的 stderr)。"""
    cmd = [sys.executable, "-m", "research_workflow.cli", f"--db={db_path}", f"--root={root}", *argv]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=engine_env(),
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, None, f"命令超时({timeout}s)"
    payload = None
    for line in reversed(done.stdout.strip().splitlines()):
        try:
            payload = json.loads(line)
            break
        except ValueError:
            continue
    return done.returncode, payload, done.stderr.strip()[-1500:]


class JobRunner:
    def __init__(self, store: WebStore, *, db_path: str, root: str, workers: int = 2) -> None:
        self.store, self.db_path, self.root = store, str(db_path), str(root)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="research-job")
        self._futures: dict[str, Any] = {}
        self._lock = threading.Lock()
        store.fail_interrupted_jobs()  # 上次进程遗留的 queued/running 不可能还在跑

    def project_dir(self, project_id: str) -> Path:
        return Path(self.root) / project_id

    def run_sync(self, argv: list[str], timeout: int = 120) -> dict[str, Any]:
        code, payload, stderr = run_engine(argv, db_path=self.db_path, root=self.root, timeout=timeout)
        if code != 0:
            logger.warning("sync engine call %s failed rc=%s stderr=%r", argv[0], code, stderr[-500:])
            raise RuntimeError(str((payload or {}).get("error") or "引擎调用失败")[:500])
        return payload or {}

    def submit(self, *, project_id: str, command: str, params: dict[str, Any], user_id: str,
               upload_path: Callable[[str], Path], attempts: int = 1, retry_of: str | None = None) -> str | None:
        """返回 job_id;项目已有活动作业返回 None(调用方应回 409)。参数不合法抛 JobValidationError。"""
        parsed = validate_params(command, params)
        argv = build_argv(command, parsed, project_id=project_id, project_dir=self.project_dir(project_id),
                          upload_path=upload_path)
        job_id = self.store.claim_project_slot(project_id=project_id, command=command, params=params,
                                               created_by=user_id, attempts=attempts, retry_of=retry_of)
        if job_id is None:
            return None
        with self._lock:
            self._futures[job_id] = self._pool.submit(self._execute, job_id, command, argv)
        return job_id

    def _execute(self, job_id: str, command: str, argv: list[str]) -> None:
        self.store.update_job(job_id, status="running", started_at=utc_now_iso())
        try:
            code, payload, stderr = run_engine(argv, db_path=self.db_path, root=self.root,
                                               timeout=COMMAND_TIMEOUTS.get(command, 600))
            if code == 0:
                self.store.update_job(job_id, status="succeeded",
                                      result_json=json.dumps(public_result(payload), ensure_ascii=False),
                                      finished_at=utc_now_iso())
            else:
                # 只回显引擎主动给出的业务错误;traceback/stderr 只进服务端日志(可能含路径与密钥片段)
                business = (payload or {}).get("error")
                logger.warning("job %s (%s) failed rc=%s stderr=%r", job_id, command, code, stderr[-800:])
                message = (str(business)[:500] if business else
                           "命令超时" if code == 124 else GENERIC_ENGINE_ERROR + job_id[:8])
                self.store.update_job(job_id, status="failed", error=message, finished_at=utc_now_iso())
        except Exception as exc:  # noqa: BLE001 - 任何异常都必须落到作业状态,不能静默丢失
            logger.exception("job %s crashed", job_id)
            self.store.update_job(job_id, status="failed", error=f"执行器异常: {type(exc).__name__}",
                                  finished_at=utc_now_iso())

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
