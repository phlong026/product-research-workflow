#!/usr/bin/env python3
"""Agent-driven CLI for the product research workflow.

每个子命令执行一个研究阶段并输出 JSON,供 AI 助手(zcode/Codex skill)解析驱动。
人工检查点通过 status 呈现、用 resolve 裁决;最终商业 Yes/No 始终由人工填写。

典型流程:
  init → ingest → [data_review 检查点] → classify → [classification_review 检查点]
  → analyze → [route_review 检查点] → dossier
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import io
from contextlib import redirect_stdout
import json
import os
import re
import sqlite3
import shutil
import tempfile
import sys
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from research_workflow.analysis import MarketResearchService
from research_workflow.analysis.models import MarketResearchReport
from research_workflow.classification import (
    ClassificationLabel,
    ClassificationSchema,
    ClassificationService,
    LLMClassificationProvider,
    RuleClassificationProvider,
)
from research_workflow.classification.models import (
    ClassificationBatch,
    ProductClassification,
)
from research_workflow.competition import build_competition_metrics
from research_workflow.competition.models import CompetitionMetricsResult
from research_workflow.data import (
    MonthlyBSRConfig,
    available_month_window,
    build_monthly_top,
    month_coverage_report,
)
from research_workflow.discovery import discover_nodes, fetch_monthly_top, merge_nodes_by_asin
from research_workflow.data.io import read_tabular_rows
from research_workflow.data.models import BSRRecord, MonthlyBSRResult
from research_workflow.data.bsr_pipeline import validate_month, _to_int
from research_workflow.connectors.base import ConnectorError
from research_workflow.connectors.sellersprite import SellerSpriteConnector
from research_workflow.domain import ProjectStatus, StepStatus
from research_workflow.domain.models import utc_now_iso
from research_workflow.dossier import (
    ResearchDossierGenerator,
    ResearchDossierInput,
    SourceArtifact,
)
from research_workflow.enrichment import (
    ASINEnrichmentService,
    SellerSpriteProductDetailProvider,
    enrich_bullets_unique_asins,
    enrich_missing_regions,
)
from research_workflow.enrichment.models import ASINProfile
from research_workflow.mcp import MCPGateway, MCPRegistry
from research_workflow.metrics import build_market_metrics
from research_workflow.metrics.models import MarketMetricsResult
from research_workflow.new_products import NewProductConfig, NewProductSnapshot, analyze_new_products
from research_workflow.new_products.models import NewProductAnalysisResult
from research_workflow.router import ResearchRouter
from research_workflow.router.models import ResearchRoutePlan
from research_workflow.store import SQLiteStore
from research_workflow.workflow import WorkflowService

MONTH_IN_FILENAME = re.compile(r"(20\d{2})[-_.]?(\d{2})")


def _today() -> date:
    """可被测试替换的当前日期(月份窗口自动化用)。"""
    return date.today()


def _safe_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]", "_", value)


# --- JSON <-> dataclass 重建(所有研究模型均为扁平 dataclass) ---

def _coerce(hint: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(hint)
    if origin is list:
        args = get_args(hint)
        if args and dataclasses.is_dataclass(args[0]):
            return [rebuild(args[0], item) for item in value]
        return value
    if origin is dict:
        args = get_args(hint)
        if len(args) == 2 and dataclasses.is_dataclass(args[1]):
            return {key: rebuild(args[1], item) for key, item in value.items()}
        return value
    if dataclasses.is_dataclass(hint):
        return rebuild(hint, value)
    return value


def rebuild(cls, data: dict[str, Any]):
    """从 asdict() 产生的 JSON 字典重建 dataclass(缺省字段走类默认值)。"""
    hints = get_type_hints(cls)
    kwargs = {}
    for field in dataclasses.fields(cls):
        if field.name not in data:
            continue
        kwargs[field.name] = _coerce(hints.get(field.name), data[field.name])
    return cls(**kwargs)


# --- 公共工具 ---

def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _service(args: argparse.Namespace) -> WorkflowService:
    if not hasattr(args, "_workflow_service"):
        args._workflow_service = WorkflowService(SQLiteStore(args.db))
    return args._workflow_service


def _project_dir(args: argparse.Namespace, project_id: str) -> Path:
    path = Path(args.root) / project_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def _require_file(path: Path, message: str) -> Any:
    if not path.exists():
        raise ValueError(f"{message}: {path}")
    return _load_json(path)


def _snapshot_source(pdir: Path, path: Path) -> Path:
    raw = path.read_bytes()
    dest = pdir / "raw" / (sha256(raw).hexdigest() + path.suffix.lower())
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if _sha256_file(dest) != sha256(raw).hexdigest():
            raise ValueError("原始快照损坏;请保留现场并恢复正确快照后重试")
    else:
        with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.replace(dest)
    return dest


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load_dotenv(path: Path) -> None:
    """把引擎 .env 中的键值注入环境(不覆盖已有变量);无第三方依赖。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip().strip('"').strip("'")
        if key.strip() and value:
            os.environ.setdefault(key.strip(), value)


def _build_gateway(config_path: Path) -> MCPGateway:
    """从引擎配置构建 MCP 网关;测试通过替换本函数注入假网关。"""
    registry_path = config_path if config_path.exists() else ROOT / "config" / "mcp.example.json"
    return MCPGateway(MCPRegistry.load(registry_path))


def _build_llm_complete():
    """构建 LLM 补全函数(仅用于分类);测试通过替换本函数注入假模型。

    需要可选依赖 anthropic(`pip install -e '.[llm]'`)与环境变量 ANTHROPIC_API_KEY;
    模型由 RESEARCH_LLM_MODEL 指定。真实调用的输出稳定性尚未做生产验收。
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ValueError("LLM 分类需要环境变量 ANTHROPIC_API_KEY(只走环境变量,勿写入文件)")
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - 取决于本机是否安装可选依赖
        raise ValueError("LLM 分类需要可选依赖: uv pip install -e '.[llm]'") from exc
    client = anthropic.AsyncAnthropic(timeout=60.0, max_retries=0)
    model = os.environ.get("RESEARCH_LLM_MODEL", "claude-sonnet-5-5")

    async def complete(prompt: str) -> str:
        message = await client.messages.create(
            model=model, max_tokens=8192, temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in message.content if getattr(block, "type", "") == "text")

    return complete


def _resolve_month(args: argparse.Namespace, path: Path) -> str:
    if args.month:
        if len(args.file) > 1:
            raise SystemExit(_emit({"error": "--month 仅在导入单个文件时可用"}) or 1)
        return args.month
    match = MONTH_IN_FILENAME.search(path.stem)
    if not match:
        raise SystemExit(
            _emit({"error": f"无法从文件名推断月份: {path.name};请用 --month YYYY-MM"}) or 1
        )
    return f"{match.group(1)}-{match.group(2)}"


def _load_records(pdir: Path, *, overlay: bool = True) -> list[BSRRecord]:
    by_month: dict[str, list[dict[str, Any]]] = _require_file(
        pdir / "records.json", "尚无数据;先运行 ingest"
    )
    records: list[BSRRecord] = []
    for month in sorted(by_month):
        records.extend(rebuild(BSRRecord, row) for row in by_month[month])
    enrichment_path = pdir / "enrichment.json"
    if overlay and enrichment_path.is_file():
        # 补齐只填 UNKNOWN 地区,不改人工审过的 records.json(其哈希已绑定 data_review)。
        regions = _load_json(enrichment_path).get("regions", {})
        for record in records:
            if record.seller_region in ("", "UNKNOWN") and regions.get(record.asin):
                record.seller_region = regions[record.asin]
    return records


def _parse_kv(items: list[str], what: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        key, _, value = item.partition("=")
        if not key.strip() or not value.strip():
            raise SystemExit(_emit({"error": f"非法 {what} 格式(应为 key=value): {item}"}) or 1)
        parsed[key.strip()] = value.strip()
    return parsed


STAGES = ["collect_bsr", "data_review", "enrich_asins", "classify", "classification_review",
          "apply_classification_review", "market_metrics", "market_analysis", "competition_metrics",
          "new_product_analysis", "research_route", "route_review", "research_route_review",
          "apply_route_review", "dossier"]
DERIVED_FILES = {"classify": ["classification.json", "enrichment.json"], "market_metrics": ["market_metrics.json",
                 "market_analysis.json", "competition.json", "new_products.json", "route_plan.json",
                 "analysis_context.json"]}


def _hashes(pdir: Path, names: list[str]) -> dict[str, str | None]:
    return {name: _sha256_file(pdir / name) if (pdir / name).is_file() else None for name in names}


def _verify_hashes(pdir: Path, hashes: dict[str, str | None]) -> None:
    if not hashes:
        raise ValueError("旧检查点未绑定工件;请重新执行对应阶段并人工复核")
    for name, digest in hashes.items():
        actual = _sha256_file(pdir / name) if (pdir / name).is_file() else None
        if Path(name).name != name or actual != digest:
            raise ValueError(f"已审工件发生变化: {name};请重跑对应阶段并人工复核")


def _require_approved(svc: WorkflowService, pid: str, pdir: Path, steps: list[str]) -> None:
    state = svc.store.get_state(pid)
    for step in steps:
        cp_id = state.get("approved_checkpoints", {}).get(step)
        cp = svc.store.get_checkpoint(cp_id) if cp_id else None
        if cp is None or cp.status.value != "approved" or step not in state.get("completed_steps", []):
            raise ValueError(f"{step} 尚未人工确认;请先检查 status 并 resolve 当前检查点")
        _verify_hashes(pdir, (cp.response or {}).get("artifact_hashes", {}))


def _invalidate(svc: WorkflowService, pid: str, pdir: Path, start: str) -> None:
    stages = STAGES[STAGES.index(start):]
    svc.invalidate_steps(pid, stages)
    for stage, names in DERIVED_FILES.items():
        if stage in stages:
            for name in names:
                (pdir / name).unlink(missing_ok=True)


# --- 子命令 ---

def cmd_init(args: argparse.Namespace) -> None:
    svc = _service(args)
    project = svc.create_project(
        args.product,
        args.marketplace,
        primary_keyword=args.primary_keyword,
        main_node=args.main_node,
    )
    svc.start(project.id)
    svc.set_step(project.id, "collect_bsr", StepStatus.RUNNING)
    pdir = _project_dir(args, project.id)
    _emit(
        {
            "project_id": project.id,
            "product": project.product,
            "marketplace": project.marketplace,
            "artifact_dir": str(pdir),
            "next": "ingest",
        }
    )


def _persist_ingest(
    svc: WorkflowService,
    args: argparse.Namespace,
    pdir: Path,
    results: list[MonthlyBSRResult],
    sources: dict[str, Any],
    source_mode: str,
    coverage: dict[str, Any] | None = None,
) -> None:
    by_month: dict[str, list[dict[str, Any]]] = (
        _load_json(pdir / "records.json") if (pdir / "records.json").exists() else {}
    )
    quality: dict[str, Any] = (
        _load_json(pdir / "quality.json") if (pdir / "quality.json").exists() else {}
    )
    all_sources: dict[str, Any] = (
        _load_json(pdir / "sources.json") if (pdir / "sources.json").exists() else {}
    )
    summary: dict[str, Any] = {}
    for result in results:
        by_month[result.month] = [record.to_dict() for record in result.records]
        quality[result.month] = {
            "stats": dataclasses.asdict(result.stats),
            "quality": dataclasses.asdict(result.quality),
            "dedupe_audit": [entry.to_dict() for entry in result.dedupe_audit],
        }
        summary[result.month] = {
            "input_rows": result.stats.input_rows,
            "output_rows": result.stats.output_rows,
            "duplicate_parent_rows_removed": result.quality.duplicate_parent_rows_removed,
            "duplicate_title_rows_removed": result.quality.duplicate_title_rows_removed,
            "warnings": result.quality.warning_count,
        }
    replaced_months = {result.month for result in results}
    all_sources = {key: value for key, value in all_sources.items()
                   if key.split(":", 1)[0] not in replaced_months}
    all_sources.update(sources)
    if not results or any(not result.records for result in results):
        raise ValueError("每个导入月份必须有有效数据;空数据不能作为零销量市场进入分析")
    if any(result.quality.invalid_sales_rows for result in results):
        raise ValueError("存在缺失/非法销量;请修正源数据后重新导入，不能将缺失视为零销量")
    _invalidate(svc, args.project, pdir, "collect_bsr")

    _save_json(pdir / "records.json", by_month)
    _save_json(pdir / "quality.json", quality)
    _save_json(pdir / "sources.json", all_sources)
    if coverage is not None:
        _save_json(pdir / "coverage.json", coverage)
    svc.set_step(args.project, "collect_bsr", StepStatus.COMPLETED, output_ref="records.json")
    checkpoint = svc.request_human_checkpoint(
        args.project,
        "data_review",
        "请检查清洗后的月度数据(行数、去重、质量警告),确认后继续。",
        {"months": summary, "sources": list(all_sources.values()), "source_mode": source_mode,
         "missing_months": sorted(coverage["missing"]) if coverage else [],
         "artifact_hashes": _hashes(pdir, ["records.json", "quality.json", "sources.json"]
                                    + (["coverage.json"] if coverage is not None else []))},
    )
    _emit(
        {
            "project_id": args.project,
            "source_mode": source_mode,
            "months": summary,
            "records_file": str(pdir / "records.json"),
            **({"missing_months": sorted(coverage["missing"]),
                "coverage": {"complete": coverage["complete"], "expected": len(coverage["expected"]),
                             "present": len(coverage["present"])},
                "nodes": coverage["nodes"]} if coverage is not None else {}),
            "checkpoint_id": checkpoint.id,
            "checkpoint_step": "data_review",
            "next": "resolve --approve 后进入 classify",
        }
    )


async def _open_connector() -> SellerSpriteConnector:
    _load_dotenv(ROOT / ".env")
    gateway = _build_gateway(ROOT / "config" / "mcp.json")
    connector = SellerSpriteConnector(gateway)
    await connector.validate()
    try:
        session = await connector.session_status()
        if not isinstance(session, dict) or not (session.get("ready") is True or session.get("loggedIn") is True):
            raise ConnectorError("SellerSprite session is not ready; login locally first")
    except ConnectorError as exc:
        raise SystemExit(
            _emit(
                {
                    "error": f"SellerSprite 会话不可用: {exc}",
                    "remedy": "先运行 setup_mcp.py 安装 MCP,并按 docs/local/MCP_MAC_SETUP.md 启动 CDP Chrome 登录;或先运行 login 子命令",
                }
            )
            or 1
        )
    return connector


def _live_months(spec: str) -> tuple[list[str], bool]:
    """返回 (月份列表, 是否自动窗口)。auto = 近三年可得月份窗口(缺月允许,显式报告)。"""
    if spec.strip().lower() == "auto":
        return available_month_window(_today()), True
    months = list(dict.fromkeys(m.strip() for m in spec.split(",") if m.strip()))
    if not months or len(months) > 60:
        raise ValueError("--months 必须提供 1 至 60 个有效月份")
    for month in months:
        validate_month(month)
    return months, False


async def _fetch_live(args: argparse.Namespace, pdir: Path) -> tuple[list[MonthlyBSRResult], dict[str, Any], dict[str, Any]]:
    months, auto = _live_months(args.months)
    nodes = list(dict.fromkeys(n.strip() for n in (args.nodes or "").split(",") if n.strip()))
    if not 1 <= args.max_pages <= 20 or not 1 <= args.page_size <= 100 or args.top_n < 1:
        raise ValueError("分页上限为 20，每页 1 至 100，top-n 必须大于零")
    if args.candidate_n < args.top_n:
        raise ValueError("--candidate-n(候选池)不能小于 --top-n")
    connector = await _open_connector()
    scopes: list[str | None] = nodes or [None]
    fetched = {}
    for scope in scopes:
        fetched[scope] = await fetch_monthly_top(
            connector, marketplace=args.marketplace, keyword=args.keyword, months=months,
            node_id_paths=[scope] if scope else None, top_n=args.candidate_n,
            page_size=args.page_size, max_pages=args.max_pages)

    results: list[MonthlyBSRResult] = []
    sources: dict[str, Any] = {}
    missing: dict[str, str] = {}
    partial_nodes: dict[str, list[str]] = {}
    raw_dir = pdir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for month in months:
        have = {scope: f for scope, f in fetched.items() if month in f.rows_by_month}
        if not have:
            missing[month] = "; ".join(sorted({f.missing_months.get(month, "no data") for f in fetched.values()}))
            continue
        lacking = sorted(str(scope) for scope in fetched if scope not in have)
        if lacking:
            partial_nodes[month] = lacking
        for scope, f in have.items():
            suffix = f"__{_safe_name(scope)}" if scope else ""
            raw_path = raw_dir / f"{month}{suffix}.json"
            _save_json(raw_path, {"provider": "sellersprite", "operation": "product_research",
                                  "rows": list(f.rows_by_month[month]), "response_pages": f.raw_by_month[month],
                                  "request_scope": f.requests[month]})
            snapshot = _snapshot_source(pdir, raw_path)
            artifact_id = f"{month}:sellersprite" + (f":{scope}" if scope else "")
            sources[artifact_id] = {
                "artifact_id": artifact_id,
                "name": f"sellersprite product_research {month}" + (f" node={scope}" if scope else ""),
                "kind": "mcp",
                "source": snapshot.relative_to(pdir).as_posix(),
                "sha256": _sha256_file(snapshot),
            }
        if len(have) > 1:  # 分散型产品:多节点样本按 ASIN 合并去重
            rows = list(merge_nodes_by_asin({str(s): list(f.rows_by_month[month]) for s, f in have.items()}).items)
        else:
            rows = list(next(iter(have.values())).rows_by_month[month])
        results.append(build_monthly_top(rows, month, MonthlyBSRConfig(top_n=args.top_n)))

    if missing and not auto:
        raise ValueError(f"显式指定的月份无数据,不能静默跳过: {sorted(missing)};请改月份或使用 --months auto")
    if not results:
        raise ValueError("所有月份均无数据;检查关键词、站点、节点或登录状态")
    if all(result.stats.parsed_rows == 0 for result in results):
        raise SystemExit(
            _emit(
                {
                    "error": "所有月份解析出 0 行:字段映射可能不匹配",
                    "remedy": f"检查 {raw_dir}/<月份>.json 的原始字段名,把实际销量/排名列名补充进 bsr_pipeline.ALIASES 后重试",
                }
            )
            or 1
        )
    coverage = month_coverage_report(months, [r.month for r in results]).to_dict()
    coverage.update({"missing_reasons": missing, "partial_node_months": partial_nodes, "nodes": nodes,
                     "window": "auto" if auto else "explicit"})
    return results, sources, coverage


def cmd_discover_nodes(args: argparse.Namespace) -> None:
    svc = _service(args)
    project = svc.store.get_project(args.project)
    if project is None:
        raise KeyError(f"Unknown project: {args.project}")
    keyword = args.keyword or project.primary_keyword
    if not keyword:
        raise ValueError("必须提供 --keyword 或项目 primary_keyword")
    if args.month:
        validate_month(args.month)
    pdir = _project_dir(args, args.project)

    async def run():
        connector = await _open_connector()
        return await discover_nodes(connector, marketplace=project.marketplace, keyword=keyword, month=args.month)

    result = asyncio.run(run())
    payload = dataclasses.asdict(result)
    payload.update({"keyword": keyword, "month": args.month, "project_id": args.project})
    _save_json(pdir / "nodes.json", payload)
    _emit({**payload, "next": "人工确认主节点/多节点后,用 ingest --live --nodes <node_id,...> 取数"})


def cmd_ingest(args: argparse.Namespace) -> None:
    svc = _service(args)
    project = svc.store.get_project(args.project)
    if project is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)

    if args.live:
        if not args.months:
            raise SystemExit(_emit({"error": "live 模式必须用 --months 指定月份,如 2025-01,2026-01"}) or 1)
        if args.marketplace and args.marketplace.upper() != project.marketplace:
            raise ValueError("live 站点必须与项目站点一致;其它站点请新建项目")
        args.marketplace = project.marketplace
        args.keyword = args.keyword or project.primary_keyword
        if not args.keyword:
            raise ValueError("live 模式必须提供 --keyword 或项目 primary_keyword，避免无范围取数")
        results, sources, coverage = asyncio.run(_fetch_live(args, pdir))
        _persist_ingest(svc, args, pdir, results, sources, source_mode="mcp:sellersprite", coverage=coverage)
        return

    if not args.months:
        args.months = None
    if not args.file:
        raise SystemExit(_emit({"error": "文件模式必须至少提供一个 --file;直连模式用 --live"}) or 1)
    results: list[MonthlyBSRResult] = []
    sources: dict[str, Any] = {}
    for file_name in args.file:
        path = Path(file_name)
        if not path.exists():
            raise KeyError(f"文件不存在: {file_name}")
        month = _resolve_month(args, path)
        snapshot = _snapshot_source(pdir, path)
        rows = read_tabular_rows(snapshot)
        results.append(build_monthly_top(rows, month, MonthlyBSRConfig(top_n=args.top_n)))
        artifact_id = f"{month}:{path.name}"
        sources[artifact_id] = {
            "artifact_id": artifact_id,
            "name": path.name,
            "kind": path.suffix.lstrip(".") or "file",
            "source": snapshot.relative_to(pdir).as_posix(),
            "sha256": _sha256_file(snapshot),
        }
    _persist_ingest(svc, args, pdir, results, sources, source_mode="file")


def cmd_login(args: argparse.Namespace) -> None:
    _load_dotenv(ROOT / ".env")
    connector = SellerSpriteConnector(_build_gateway(ROOT / "config" / "mcp.json"))
    async def login():
        await connector.validate()
        return await connector.login(wait_seconds=args.wait)
    result = asyncio.run(login())
    _emit({"login": "completed" if result.get("ready") is True else "pending",
           "ready": result.get("ready") is True,
           "next": "ingest --live" if result.get("ready") is True else "请在本机浏览器完成登录后重新检查"})


def _run_enrichment(args: argparse.Namespace, project, records: list[BSRRecord],
                    profiles: dict[str, ASINProfile]) -> dict[str, Any]:
    """地区(T1-6)与卖点(T1-7)补齐:各只取需要的 ASIN,冲突/失败进入审阅,不静默覆盖。"""
    async def run():
        connector = await _open_connector()
        service = ASINEnrichmentService(SellerSpriteProductDetailProvider(connector))
        regions = await enrich_missing_regions(profiles, service, project.marketplace)
        by_month: dict[str, list[BSRRecord]] = {}
        for record in records:
            by_month.setdefault(record.month, []).append(record)
        bullets = await enrich_bullets_unique_asins(by_month, service, project.marketplace)
        return regions, bullets

    regions, bullets = asyncio.run(run())
    for asin, profile in profiles.items():
        done = regions.profiles.get(asin)
        if done is not None:
            profile.seller_region = done.seller_region
            profile.attributes.update(done.attributes)
        profile.bullets = list(bullets.bullets_by_asin.get(asin, profile.bullets))
    return {
        "regions": {asin: p.seller_region for asin, p in regions.profiles.items()
                    if p.seller_region not in ("", "UNKNOWN")},
        "bullets": {asin: list(v) for asin, v in bullets.bullets_by_asin.items()},
        "summary": {
            "unknown_region_before": regions.unknown_before,
            "unknown_region_after": regions.unknown_after,
            "regions_filled": regions.filled,
            "bullets_fetched_asins": len(bullets.bullets_by_asin),
            "bullets_missing_asins": sorted(bullets.missing_asins),
        },
        "conflicts": [dataclasses.asdict(c) for c in [*regions.conflicts, *bullets.conflicts]],
        "failures": [dataclasses.asdict(f) for f in [*regions.failures, *bullets.failures]],
    }


def cmd_classify(args: argparse.Namespace) -> None:
    svc = _service(args)
    project = svc.store.get_project(args.project)
    if project is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)
    _require_approved(svc, args.project, pdir, ["data_review"])
    records = _load_records(pdir, overlay=False)  # 用原始记录重算,避免叠加旧的补齐结果

    profiles: dict[str, ASINProfile] = {}
    for record in records:
        profiles.setdefault(
            record.asin,
            ASINProfile(
                asin=record.asin,
                title=record.title,
                brand=record.brand,
                seller_region=record.seller_region or "UNKNOWN",
                launch_date=record.launch_date,
                image_url=record.image_url,
            ),
        )
    enrichment = _run_enrichment(args, project, records, profiles) if args.enrich else None

    labels = [
        ClassificationLabel(key=key, name=name)
        for key, name in _parse_kv(args.label, "--label").items()
    ]
    schema = ClassificationSchema(project_id=args.project, dimension=args.dimension, labels=labels)
    if args.provider == "llm":
        provider = LLMClassificationProvider(_build_llm_complete())
    else:
        rules = {
            key: [word.strip() for word in value.split(",") if word.strip()]
            for key, value in _parse_kv(args.rule, "--rule").items()
        } if args.rule else {}
        provider = RuleClassificationProvider(rules)
    batch = asyncio.run(ClassificationService(provider).classify(schema, list(profiles.values())))

    _invalidate(svc, args.project, pdir, "enrich_asins")
    if enrichment is not None:
        _save_json(pdir / "enrichment.json", enrichment)
    svc.set_step(args.project, "enrich_asins", StepStatus.COMPLETED if enrichment else StepStatus.SKIPPED)
    _save_json(pdir / "classification.json", {"schema": schema.canonical_payload(), "batch": batch.to_dict(),
                                              "provider": provider.name})
    svc.set_step(args.project, "classify", StepStatus.COMPLETED, output_ref="classification.json")
    needs_review = sorted(asin for asin, row in batch.rows.items() if row.needs_review)
    enrichment_note = "已执行地区/卖点补齐,冲突见 enrichment.json" if enrichment else "未执行 ASIN 补全"
    checkpoint = svc.request_human_checkpoint(args.project, "classification_review",
        f"请检查{'LLM' if provider.name == 'llm' else '规则'}分类结果；待人工行必须逐一指定分类后才能确认。{enrichment_note}。",
        {"schema": schema.canonical_payload(), "classification": batch.to_dict(),
         "needs_review": needs_review,
         "enrichment": ({"summary": enrichment["summary"], "conflicts": enrichment["conflicts"],
                         "failures": enrichment["failures"]} if enrichment else None),
         "artifact_hashes": _hashes(pdir, ["classification.json"] + (["enrichment.json"] if enrichment else []))})

    counts: dict[str, int] = {}
    for row in batch.rows.values():
        counts[row.label] = counts.get(row.label, 0) + 1
    _emit(
        {
            "project_id": args.project,
            "classification_version": batch.version,
            "classification_provider": provider.name,
            "enrichment": enrichment["summary"] if enrichment else "skipped",
            "enrichment_conflicts": len(enrichment["conflicts"]) if enrichment else 0,
            "label_counts": counts,
            "needs_review": needs_review,
            "total_asins": len(batch.rows),
            "checkpoint_id": checkpoint.id,
            "checkpoint_step": "classification_review",
            "next": "resolve --approve(可用 --edit ASIN=标签 修改;待人工行必须指定)后进入 analyze",
        }
    )


def cmd_status(args: argparse.Namespace) -> None:
    svc = _service(args)
    snapshot = svc.snapshot(args.project)
    pdir = Path(args.root) / args.project
    pending: dict[str, Any] | None = None
    checkpoint_id = snapshot["state"].get("pending_checkpoint_id")
    if checkpoint_id:
        checkpoint = svc.store.get_checkpoint(checkpoint_id)
        if checkpoint is not None and checkpoint.status.value == "pending":
            pending = {
                "checkpoint_id": checkpoint.id,
                "step_key": checkpoint.step_key,
                "prompt": checkpoint.prompt,
                "payload": checkpoint.payload,
            }

    artifacts = sorted(p.name for p in pdir.glob("*.json")) if pdir.exists() else []
    completed = set(snapshot["state"].get("completed_steps", []))
    if pending:
        next_action = "resolve"
    elif "data_review" not in completed:
        next_action = "ingest"
    elif "classification_review" not in completed:
        next_action = "classify"
    elif "research_route_review" not in completed:
        next_action = "analyze"
    elif "dossier" not in completed:
        next_action = "dossier"
    else:
        next_action = "done"
    _emit(
        {
            **snapshot,
            "pending_checkpoint": pending,
            "artifacts": artifacts,
            "next": next_action,
        }
    )


def cmd_resolve(args: argparse.Namespace) -> None:
    if args.approve == args.reject:
        raise SystemExit(
            _emit({"error": "必须且只能指定 --approve 或 --reject 之一"}) or 1
        )
    svc = _service(args)
    checkpoint = svc.store.get_checkpoint(args.checkpoint)
    if checkpoint is None or checkpoint.project_id != args.project:
        raise KeyError(f"Unknown checkpoint: {args.checkpoint}")
    if svc.store.get_state(args.project).get("pending_checkpoint_id") != checkpoint.id:
        raise ValueError("检查点已过期;先 status 查看当前待确认项")
    if checkpoint.status.value != "pending":
        raise ValueError(f"Checkpoint 已裁决: {checkpoint.status.value}")

    pdir = _project_dir(args, args.project)
    response: dict[str, Any] = {"approved": args.approve}
    if args.approve:
        _verify_hashes(pdir, checkpoint.payload.get("artifact_hashes", {}))

    if args.approve and checkpoint.step_key == "classification_review":
        envelope = _require_file(pdir / "classification.json", "缺少 classification.json;先运行 classify")
        schema = rebuild(ClassificationSchema, envelope["schema"])
        batch = rebuild(ClassificationBatch, envelope["batch"])
        edits = _parse_kv(args.edit, "--edit") if args.edit else {}
        # 空编辑也必须经过 apply_human_edits,保证 approved 标志被置位。
        batch = ClassificationService(RuleClassificationProvider({})).apply_human_edits(
            schema, batch, edits
        )
        unresolved = sorted(asin for asin, row in batch.rows.items() if row.needs_review)
        if unresolved:
            raise ValueError(f"仍有待人工分类的 ASIN,请用 --edit ASIN=标签 逐一指定: {unresolved}")
        if edits:
            response["edits"] = edits
        _save_json(pdir / "classification.json", {**envelope, "batch": batch.to_dict()})
        svc.set_step(args.project, "apply_classification_review", StepStatus.COMPLETED)

    if args.approve and checkpoint.step_key in {"route_review", "research_route_review"}:
        plan_data = _require_file(pdir / "route_plan.json", "缺少 route_plan.json;先运行 analyze")
        plan = rebuild(ResearchRoutePlan, plan_data)
        selected = [item.strip() for item in (args.select or "").split(",") if item.strip()]
        try:
            plan = ResearchRouter().confirm(plan, selected)
        except ValueError as exc:
            raise SystemExit(_emit({"error": str(exc)}) or 1)
        response["selected_routes"] = selected
        _save_json(pdir / "route_plan.json", plan.to_dict())
        svc.set_step(args.project, "apply_route_review", StepStatus.COMPLETED)

    if args.approve:
        response["artifact_hashes"] = _hashes(pdir, list(checkpoint.payload["artifact_hashes"]))
    svc.resolve_human_checkpoint(args.checkpoint, approved=args.approve, response=response)

    next_action = {
        "data_review": "classify",
        "classification_review": "analyze",
        "route_review": "dossier",
        "research_route_review": "dossier",
    }.get(checkpoint.step_key, "status")
    if not args.approve:
        next_action = {"data_review": "ingest", "classification_review": "classify"}.get(checkpoint.step_key, "analyze")
    _emit(
        {
            "project_id": args.project,
            "checkpoint_id": args.checkpoint,
            "step_key": checkpoint.step_key,
            "resolution": "approved" if args.approve else "rejected",
            "selected_routes": response.get("selected_routes", []),
            "edits": response.get("edits", {}),
            "next": next_action,
        }
    )


def cmd_analyze(args: argparse.Namespace) -> None:
    svc = _service(args)
    if svc.store.get_project(args.project) is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)
    _require_approved(svc, args.project, pdir, ["data_review", "classification_review"])
    envelope = _require_file(
        pdir / "classification.json", "缺少 classification.json;先运行 classify 并通过人工确认"
    )
    if not envelope["batch"].get("approved"):
        raise ValueError("分类尚未人工确认;先 resolve classification_review 检查点")
    records = _load_records(pdir)
    classifications = {asin: row["label"] for asin, row in envelope["batch"]["rows"].items()}

    pairs = [tuple(item.split(":", 1)) for item in args.pair]
    periods = [tuple(item.split(":", 3)) for item in args.period]
    market = build_market_metrics(
        records,
        classifications,
        decomposition_pairs=pairs or None,
        period_comparison_pairs=periods or None,
    )
    analysis = asyncio.run(MarketResearchService().analyze(market))
    competition = build_competition_metrics(records)

    new_products = None
    if args.new_products_file:
        new_source = _snapshot_source(pdir, Path(args.new_products_file))
        rows = read_tabular_rows(new_source)
        snapshots = [
            NewProductSnapshot(
                asin=str(_col(row, "ASIN", "asin")).strip().upper(),
                month=str(_col(row, "月份", "month")),
                monthly_sales=_sales(_col(row, "月销量", "monthly_sales", "销量", "sales")),
                launch_date=_col(row, "上架时间", "launch_date") or None,
                seller_region=str(_col(row, "卖家所属地", "seller_region") or "UNKNOWN"),
                account_type=str(_col(row, "账号类型", "account_type") or "UNKNOWN"),
                segment=str(_col(row, "细分", "segment") or "unknown"),
                eligible=_eligible(_col(row, "eligible", "纳入研究")),
            )
            for row in rows
            if str(_col(row, "ASIN", "asin")).strip()
        ]
        new_products = analyze_new_products(
            snapshots,
            NewProductConfig(
                sales_threshold=args.sales_threshold,
                consecutive_months=args.consecutive_months,
                early_threshold_window_months=args.early_window,
                current_survival_threshold=args.survival_threshold,
            ),
        )

    route_plan = ResearchRouter().suggest(market, competition, new_products)

    _invalidate(svc, args.project, pdir, "market_metrics")
    _save_json(pdir / "market_metrics.json", market.to_dict())
    _save_json(pdir / "market_analysis.json", analysis.to_dict())
    _save_json(pdir / "competition.json", competition.to_dict())
    if new_products is not None:
        _save_json(pdir / "new_products.json", new_products.to_dict())
    _save_json(pdir / "route_plan.json", route_plan.to_dict())
    _save_json(pdir / "analysis_context.json", {
        "classification_version": envelope["batch"]["version"],
        "classification_provider": envelope.get("provider", "rules"), "analysis_provider": analysis.provider,
        "enrichment": "done" if (pdir / "enrichment.json").is_file() else "skipped", "pairs": pairs, "periods": periods,
        "new_products_source": ({"artifact_id": "new-products", "name": Path(args.new_products_file).name,
            "kind": new_source.suffix.lstrip("."), "source": new_source.relative_to(pdir).as_posix(),
            "sha256": _sha256_file(new_source)} if args.new_products_file else None)})

    svc.set_step(args.project, "market_metrics", StepStatus.COMPLETED, output_ref="market_metrics.json")
    svc.set_step(args.project, "market_analysis", StepStatus.COMPLETED, output_ref="market_analysis.json")
    svc.set_step(args.project, "competition_metrics", StepStatus.COMPLETED, output_ref="competition.json")
    svc.set_step(
        args.project,
        "new_product_analysis",
        StepStatus.COMPLETED if new_products is not None else StepStatus.SKIPPED,
    )
    svc.set_step(args.project, "research_route", StepStatus.COMPLETED, output_ref="route_plan.json")
    checkpoint = svc.request_human_checkpoint(args.project, "research_route_review",
        "请确认下一步研究分支。确认只登记研究路线，不自动执行支线。",
        {**route_plan.to_dict(), "artifact_hashes": _hashes(pdir, ["market_metrics.json",
            "market_analysis.json", "competition.json", "new_products.json", "route_plan.json", "analysis_context.json"])})

    latest = market.market[-1] if market.market else None
    _emit(
        {
            "project_id": args.project,
            "latest_market": dataclasses.asdict(latest) if latest else None,
            "route_suggestions": [
                {"route_id": s.route_id, "title": s.title, "reason": s.reason, "priority": s.priority}
                for s in route_plan.suggestions
            ],
            "checkpoint_id": checkpoint.id,
            "checkpoint_step": "route_review",
            "next": "resolve --approve --select <route_id,...> 后进入 dossier",
        }
    )


def _sales(value: Any) -> int:
    result = _to_int(value, None)
    if result is None:
        raise ValueError("新品月销量必须是非负整数，缺失不得视为零")
    return result


def _eligible(value: Any) -> bool:
    normalized = str(value).strip().casefold()
    if normalized in {"", "true", "1", "yes", "是"}:
        return True
    if normalized in {"false", "0", "no", "否"}:
        return False
    raise ValueError("eligible 必须为 true/false、1/0 或 是/否")


def _col(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip() != "":
            return value
    return ""


def cmd_dossier(args: argparse.Namespace) -> None:
    svc = _service(args)
    project = svc.store.get_project(args.project)
    if project is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)
    _require_approved(svc, args.project, pdir, ["data_review", "classification_review", "research_route_review"])
    plan_data = _require_file(pdir / "route_plan.json", "缺少 route_plan.json;先运行 analyze")
    if not plan_data.get("human_confirmed"):
        raise ValueError("研究路线尚未人工确认;先 resolve route_review 检查点")

    market = rebuild(MarketMetricsResult, _require_file(pdir / "market_metrics.json", "缺少 market_metrics.json"))
    analysis = rebuild(MarketResearchReport, _load_json(pdir / "market_analysis.json"))
    competition = rebuild(CompetitionMetricsResult, _load_json(pdir / "competition.json"))
    new_products = (
        rebuild(NewProductAnalysisResult, _load_json(pdir / "new_products.json"))
        if (pdir / "new_products.json").exists()
        else None
    )
    route_plan = rebuild(ResearchRoutePlan, plan_data)
    source_items = list(_load_json(pdir / "sources.json").values())
    context = _load_json(pdir / "analysis_context.json")
    if context.get("new_products_source"):
        source_items.append(context["new_products_source"])
    sources = [rebuild(SourceArtifact, item) for item in source_items]
    for source in sources:
        source_path = Path(source.source)
        if not source_path.is_absolute():
            source_path = pdir / source_path
        if not source_path.is_file() or _sha256_file(source_path) != source.sha256:
            raise ValueError(f"原始资料缺失或哈希不匹配: {source.artifact_id};请重新导入")

    quality = _load_json(pdir / "quality.json")
    data_quality = [
        {
            "label": f"{month} 清洗结果",
            "value": (
                f"输入 {item['stats']['input_rows']} 行 → 输出 {item['stats']['output_rows']} 行,"
                f"质量警告 {sum(v for k, v in item['quality'].items() if k != 'duplicate_title_rows_removed' and k != 'duplicate_parent_rows_removed')}"
            ),
        }
        for month, item in sorted(quality.items())
    ]
    missing_evidence: list[str] = ["研究路线确认仅登记，支线尚未自动执行"]
    if context.get("classification_provider", "rules") == "rules":
        missing_evidence.append("当前采用规则分类，未调用 LLM 分类")
    if context.get("enrichment") != "done":
        missing_evidence.append("当前 CLI 未执行 ASIN 补全")
    missing_evidence.append("市场解释采用确定性模板，未调用 LLM 解释")
    if new_products is None:
        missing_evidence.append("未提供新品跟踪表(new_products),T09 新品结论缺失")

    output = Path(args.output)
    approvals = svc.store.get_state(args.project).get("approved_checkpoints", {})
    evidence_names = ["records.json", "quality.json", "sources.json", "classification.json", "analysis_context.json",
                      "market_metrics.json", "market_analysis.json", "competition.json", "new_products.json", "route_plan.json"]
    evidence_files = {name: pdir / name for name in evidence_names if (pdir / name).is_file()}
    evidence_files.update({source.sha256 + Path(source.source).suffix: (Path(source.source) if Path(source.source).is_absolute() else pdir / source.source) for source in sources})
    approval_history = {key: dataclasses.asdict(svc.store.get_checkpoint(value)) for key,value in approvals.items()}
    classification_envelope = _load_json(pdir / "classification.json")
    records_hash = _sha256_file(pdir / "records.json")
    workbook_approvals = [
        {"step": key, "checkpoint_id": item["id"], "status": item["status"]}
        for key, item in sorted(approval_history.items())
    ]
    ResearchDossierGenerator().generate(
        ResearchDossierInput(
            project_id=project.id,
            product=project.product,
            marketplace=project.marketplace,
            market_metrics=market,
            market_analysis=analysis,
            competition_metrics=competition,
            new_product_analysis=new_products,
            route_plan=route_plan,
            source_artifacts=[
                dataclasses.replace(
                    source,
                    source="evidence/" + source.sha256 + Path(source.source).suffix,
                    relative_path="evidence/" + source.sha256 + Path(source.source).suffix,
                )
                for source in sources
            ],
            records=_load_records(pdir),
            classification=rebuild(ClassificationBatch, classification_envelope["batch"]),
            data_version=f"records.json@{records_hash[:12]}",
            data_hash=records_hash,
            approval_history=workbook_approvals,
            data_quality=data_quality,
            missing_evidence=missing_evidence,
            metadata={**context, "approved_checkpoints": svc.store.get_state(args.project).get("approved_checkpoints", {})},
        ),
        output, evidence_files=evidence_files, approval_history=approval_history,
    )
    svc.set_step(args.project, "dossier", StepStatus.COMPLETED, output_ref=str(output))
    svc.store.update_project_status(args.project, ProjectStatus.COMPLETED, utc_now_iso())
    _emit(
        {
            "project_id": args.project,
            "project_status": "completed",
            "dossier_dir": str(output.resolve()),
            "report_md": str((output / "report.md").resolve()),
            "dashboard_html": str((output / "dashboard.html").resolve()),
            "files": sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()),
            "next": "向用户汇报研究发现;最终商业 Yes/No 由人工填写,系统不代填",
        }
    )


def cmd_projects(args: argparse.Namespace) -> None:
    svc = _service(args)
    _emit(
        {
            "projects": [
                {
                    "project_id": project.id,
                    "product": project.product,
                    "marketplace": project.marketplace,
                    "status": project.status.value,
                    "current_step": svc.store.get_state(project.id).get("current_step"),
                    "created_at": project.created_at,
                }
                for project in svc.store.list_projects()
            ]
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_research", description="跨境选品研究工作流(AI 助手驱动入口)"
    )
    parser.add_argument("--db", default=os.environ.get("RESEARCH_DB_PATH", str(ROOT / "runtime" / "research.db")))
    parser.add_argument("--root", default=str(ROOT / "runtime" / "projects"))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="创建研究项目")
    p.add_argument("--product", required=True)
    p.add_argument("--marketplace", required=True)
    p.add_argument("--primary-keyword")
    p.add_argument("--main-node")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("ingest", help="导入并清洗月度 BSR 数据(文件或 --live MCP 直连)")
    p.add_argument("--project", required=True)
    p.add_argument("--file", action="append", default=[], help="月度数据文件,可多次(文件模式)")
    p.add_argument("--month", help="单文件时指定月份 YYYY-MM")
    p.add_argument("--top-n", type=int, default=100)
    p.add_argument("--live", action="store_true", help="通过 MCP 直连 SellerSprite 取数")
    p.add_argument("--marketplace", help="live 模式站点;默认取项目站点")
    p.add_argument("--keyword", help="live 模式调研关键词")
    p.add_argument("--months", help="live 模式月份列表,如 2025-01,2026-01;auto = 近三年可得月份窗口(缺月显式报告)")
    p.add_argument("--nodes", help="live 模式类目节点 nodeIdPath,逗号分隔;多节点按 ASIN 合并(须先人工确认节点)")
    p.add_argument("--candidate-n", type=int, default=200, help="每月候选池大小(默认 Top200),清洗后截取 --top-n")
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--max-pages", type=int, default=20)
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("discover-nodes", help="枚举关键词样本的类目节点与 ASIN 数,给出主节点建议(须人工确认)")
    p.add_argument("--project", required=True)
    p.add_argument("--keyword")
    p.add_argument("--month", help="样本月份 YYYY-MM;默认不限月")
    p.set_defaults(func=cmd_discover_nodes)

    p = sub.add_parser("login", help="触发 SellerSprite MCP 登录流程(CDP Chrome)")
    p.add_argument("--wait", type=int, default=180)
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("classify", help="运行产品分类并请求人工复核")
    p.add_argument("--project", required=True)
    p.add_argument("--label", action="append", required=True, help="标签 key=中文名,可多次")
    p.add_argument("--dimension", default="segment")
    p.add_argument("--rule", action="append", default=[], help="规则 key=关键词1,关键词2,可多次")
    p.add_argument("--provider", choices=["rules", "llm"], default="rules",
                   help="分类提供者:rules=关键词规则(默认);llm=大模型(需 ANTHROPIC_API_KEY,歧义标待人工)")
    p.add_argument("--enrich", action="store_true", help="分类前经 SellerSprite 补齐缺失地区与卖点(需已登录)")
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("projects", help="列出全部研究项目(跨会话恢复用)")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("status", help="查看项目状态与待裁决检查点")
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("resolve", help="裁决人工检查点")
    p.add_argument("--project", required=True)
    p.add_argument("--checkpoint", required=True)
    p.add_argument("--approve", action="store_true")
    p.add_argument("--reject", action="store_true")
    p.add_argument("--edit", action="append", default=[], help="分类修改 ASIN=标签,可多次")
    p.add_argument("--select", help="确认的研究分支 route_id,逗号分隔")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("analyze", help="市场/竞争/新品确定性分析 + 研究路线建议")
    p.add_argument("--project", required=True)
    p.add_argument("--new-products-file")
    p.add_argument("--sales-threshold", type=int, default=250)
    p.add_argument("--consecutive-months", type=int, default=3)
    p.add_argument("--early-window", type=int, default=3)
    p.add_argument("--survival-threshold", type=int, default=None)
    p.add_argument("--pair", action="append", default=[], help="份额分解对 YYYY-MM:YYYY-MM")
    p.add_argument("--period", action="append", default=[], help="跨期比较 a起点:a终点:b起点:b终点")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("dossier", help="生成研究资料包")
    p.add_argument("--project", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_dossier)

    return parser


def _restore_files(backups: dict[Path, bytes]) -> None:
    for path, raw in backups.items():
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(raw)
            temporary = Path(handle.name)
        temporary.replace(path)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    backups: dict[Path, bytes] = {}
    try:
        if args.command == "login":
            args.func(args)
            return
        svc = _service(args)
        # One DB write transaction serializes a complete CLI command across processes,
        # including reviewed file reads. Status API is experimental; do not share roots.
        output = io.StringIO()
        with redirect_stdout(output):
            with svc.store.transaction(on_rollback=lambda: _restore_files(backups)):
                if args.command == "resolve":
                    pdir = Path(args.root) / args.project
                    backups = {p: p.read_bytes() for p in [pdir / "classification.json", pdir / "route_plan.json"] if p.is_file()}
                args.func(args)
        print(output.getvalue(), end="")
    except SystemExit:
        if "output" in locals():
            print(output.getvalue(), end="")
        raise
    except (KeyError, ValueError, OSError, sqlite3.Error, ConnectorError) as exc:
        _emit({"error": str(exc).strip('"'), "command": args.command})
        sys.exit(1)
    finally:
        if hasattr(args, "_workflow_service"):
            args._workflow_service.store.close()



if __name__ == "__main__":
    main()
