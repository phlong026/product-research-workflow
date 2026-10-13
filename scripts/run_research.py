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
    RuleClassificationProvider,
)
from research_workflow.classification.models import (
    ClassificationBatch,
    ProductClassification,
)
from research_workflow.enrichment.adapters import merge_profiles, profile_from_payload
from research_workflow.enrichment.service import (
    ASINEnrichmentService,
    SellerSpriteProductDetailProvider,
)
from research_workflow.competition import build_competition_metrics
from research_workflow.competition.models import CompetitionMetricsResult
from research_workflow.data import MonthlyBSRConfig, build_monthly_top
from research_workflow.data.io import read_tabular_rows
from research_workflow.data.models import BSRRecord, MonthlyBSRResult
from research_workflow.data.nodes import discover_nodes, merge_node_rows
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


def _valid_profiles(pdir: Path) -> dict[str, ASINProfile] | None:
    """返回与当前 records.json 绑定的商品补齐画像；过期/缺失返回 None（绝不使用过期补齐）。"""
    path = pdir / "profiles.json"
    if not path.exists() or not (pdir / "records.json").exists():
        return None
    doc = _load_json(path)
    if doc.get("records_sha256") != _sha256_file(pdir / "records.json"):
        return None
    return {asin: rebuild(ASINProfile, row) for asin, row in doc["profiles"].items()}


def _load_records(pdir: Path) -> list[BSRRecord]:
    by_month: dict[str, list[dict[str, Any]]] = _require_file(
        pdir / "records.json", "尚无数据;先运行 ingest"
    )
    profiles = _valid_profiles(pdir) or {}
    records: list[BSRRecord] = []
    for month in sorted(by_month):
        for row in by_month[month]:
            record = rebuild(BSRRecord, row)
            profile = profiles.get(record.asin)
            # 卖家地区补齐：只填 UNKNOWN/空白，不覆盖源数据已有值。
            if profile and record.seller_region in {"", "UNKNOWN"} and profile.seller_region not in {"", "UNKNOWN"}:
                record.seller_region = profile.seller_region
            records.append(record)
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
DERIVED_FILES = {"enrich_asins": ["profiles.json"], "classify": ["classification.json"], "market_metrics": ["market_metrics.json",
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


NO_NODE = "_all"


def source_month(artifact_id: str) -> str:
    return artifact_id.split(":", 1)[0]


def _safe_node(node: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]+", "_", node).strip("_") or "node"


def _confirmed_nodes(pdir: Path) -> list[str] | None:
    path = pdir / "nodes.json"
    if not path.exists():
        return None
    confirmed = _load_json(path).get("confirmed")
    return list(confirmed["nodes"]) if confirmed else None


def _resolve_nodes(args: argparse.Namespace, pdir: Path) -> list[str | None]:
    requested = [n.strip() for n in (getattr(args, "node", None) or []) if n.strip()]
    confirmed = _confirmed_nodes(pdir)
    if confirmed is not None:
        outside = [n for n in requested if n not in confirmed]
        if outside:
            raise ValueError(f"节点未经人工确认: {outside};先 nodes confirm，或改用已确认节点 {confirmed}")
        return requested or list(confirmed)
    return requested or [None]


def _bsr_config(args: argparse.Namespace) -> MonthlyBSRConfig:
    return MonthlyBSRConfig(
        top_n=args.top_n,
        dedupe_by_metrics=not getattr(args, "no_metric_dedupe", False),
        dedupe_exact_title=bool(getattr(args, "dedupe_title", False)),
    )


def _persist_ingest(
    svc: WorkflowService,
    args: argparse.Namespace,
    pdir: Path,
    results: list[MonthlyBSRResult],
    sources: dict[str, Any],
    source_mode: str,
    candidates: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
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
            "dedupe_audit": result.dedupe_audit or [],
        }
        summary[result.month] = {
            "input_rows": result.stats.input_rows,
            "output_rows": result.stats.output_rows,
            "duplicate_parent_rows_removed": result.quality.duplicate_parent_rows_removed,
            "duplicate_title_rows_removed": result.quality.duplicate_title_rows_removed,
            "duplicate_metric_rows_removed": result.quality.duplicate_metric_rows_removed,
            "dedupe_unjudgeable_rows": result.quality.dedupe_unjudgeable_rows,
            "warnings": result.quality.warning_count,
        }
    # 只替换同月同节点的旧来源；同月其它节点的来源必须保留（多节点合并研究）。
    replaced = {(source_month(key), value.get("node") or NO_NODE) for key, value in sources.items()
                for value in [value]}
    all_sources = {key: value for key, value in all_sources.items()
                   if (source_month(key), value.get("node") or NO_NODE) not in replaced}
    all_sources.update(sources)
    if not results or any(not result.records for result in results):
        raise ValueError("每个导入月份必须有有效数据;空数据不能作为零销量市场进入分析")
    if any(result.quality.invalid_sales_rows for result in results):
        raise ValueError("存在缺失/非法销量;请修正源数据后重新导入，不能将缺失视为零销量")
    _invalidate(svc, args.project, pdir, "collect_bsr")

    if candidates is not None:
        _save_json(pdir / "candidates.json", candidates)
    _save_json(pdir / "records.json", by_month)
    _save_json(pdir / "quality.json", quality)
    _save_json(pdir / "sources.json", all_sources)
    svc.set_step(args.project, "collect_bsr", StepStatus.COMPLETED, output_ref="records.json")
    checkpoint = svc.request_human_checkpoint(
        args.project,
        "data_review",
        "请检查清洗后的月度数据(行数、去重、质量警告),确认后继续。",
        {"months": summary, "sources": list(all_sources.values()), "source_mode": source_mode,
         "artifact_hashes": _hashes(pdir, ["records.json", "quality.json", "sources.json"])},
    )
    _emit(
        {
            "project_id": args.project,
            "source_mode": source_mode,
            "months": summary,
            "records_file": str(pdir / "records.json"),
            "checkpoint_id": checkpoint.id,
            "checkpoint_step": "data_review",
            "next": "resolve --approve 后进入 classify",
        }
    )


def _build_month_results(
    args: argparse.Namespace, pdir: Path, batches: list[tuple[str, str, list[dict[str, Any]]]]
) -> list[MonthlyBSRResult]:
    """把本次导入的 (月, 节点, 行) 并入候选池，再按月把所有节点合并后清洗出 TopN。"""
    candidates: dict[str, dict[str, list[dict[str, Any]]]] = (
        _load_json(pdir / "candidates.json") if (pdir / "candidates.json").exists() else {}
    )
    for month, node, rows in batches:
        month_nodes = candidates.setdefault(month, {})
        if node == NO_NODE and any(n != NO_NODE for n in month_nodes) or \
                node != NO_NODE and NO_NODE in month_nodes:
            raise ValueError(f"{month} 不能混用“无节点导入”与“按节点导入”；请统一口径后重新导入")
        month_nodes[node] = rows
    args._candidates = candidates
    results: list[MonthlyBSRResult] = []
    for month in dict.fromkeys(month for month, _, _ in batches):
        month_nodes = candidates[month]
        rows = month_nodes[NO_NODE] if list(month_nodes) == [NO_NODE] else merge_node_rows(month_nodes)
        results.append(build_monthly_top(rows, month, _bsr_config(args)))
    return results


async def _fetch_live(args: argparse.Namespace, pdir: Path) -> tuple[list[MonthlyBSRResult], dict[str, Any]]:
    months = list(dict.fromkeys(m.strip() for m in args.months.split(",") if m.strip()))
    if not months or len(months) > 60:
        raise ValueError("--months 必须提供 1 至 60 个有效月份")
    for month in months:
        validate_month(month)
    if not 1 <= args.max_pages <= 20 or not 1 <= args.page_size <= 100 or args.top_n < 1:
        raise ValueError("分页上限为 20，每页 1 至 100，top-n 必须大于零")
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
    sources: dict[str, Any] = {}
    raw_dir = pdir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    batches: list[tuple[str, str, list[dict[str, Any]]]] = []
    for month in months:
        for node in _resolve_nodes(args, pdir):
            response = await connector.product_research(
                marketplace=args.marketplace,
                keyword=args.keyword,
                month=month,
                node_id_paths=[node] if node else None,
                page_size=args.page_size,
                max_pages=args.max_pages,
            )
            rows = response.data.get("items", []) if isinstance(response.data, dict) else []
            tag = f"{month}__{_safe_node(node)}" if node else month
            raw_path = raw_dir / f"{tag}.json"
            _save_json(raw_path, {"provider": "sellersprite", "operation": "product_research", "rows": rows,
                                  "response_pages": response.raw,
                                  "request_scope": {"month": month, "marketplace": args.marketplace,
                                                    "keyword": args.keyword, "node": node}})
            batches.append((month, node or NO_NODE, rows))
            snapshot = _snapshot_source(pdir, raw_path)
            artifact_id = f"{month}:{node}:sellersprite" if node else f"{month}:sellersprite"
            sources[artifact_id] = {
                "artifact_id": artifact_id,
                "name": f"sellersprite product_research {month}" + (f" node={node}" if node else ""),
                "kind": "mcp",
                "node": node or NO_NODE,
                "source": snapshot.relative_to(pdir).as_posix(),
                "sha256": _sha256_file(snapshot),
            }
    results = _build_month_results(args, pdir, batches)
    if results and all(result.stats.parsed_rows == 0 for result in results):
        raise SystemExit(
            _emit(
                {
                    "error": "所有月份解析出 0 行:字段映射可能不匹配",
                    "remedy": f"检查 {raw_dir}/<月份>.json 的原始字段名,把实际销量/排名列名补充进 bsr_pipeline.ALIASES 后重试",
                }
            )
            or 1
        )
    return results, sources


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
        results, sources = asyncio.run(_fetch_live(args, pdir))
        _persist_ingest(svc, args, pdir, results, sources, source_mode="mcp:sellersprite",
                        candidates=getattr(args, "_candidates", None))
        return

    if not args.months:
        args.months = None
    if not args.file:
        raise SystemExit(_emit({"error": "文件模式必须至少提供一个 --file;直连模式用 --live"}) or 1)
    sources: dict[str, Any] = {}
    batches: list[tuple[str, str, list[dict[str, Any]]]] = []
    nodes = _resolve_nodes(args, pdir)
    if len(nodes) != 1:
        raise ValueError("文件模式一次导入只能指定一个 --node；多个节点请分次导入后自动合并")
    node = nodes[0]
    for file_name in args.file:
        path = Path(file_name)
        if not path.exists():
            raise KeyError(f"文件不存在: {file_name}")
        month = _resolve_month(args, path)
        snapshot = _snapshot_source(pdir, path)
        rows = read_tabular_rows(snapshot)
        batches.append((month, node or NO_NODE, rows))
        artifact_id = f"{month}:{node}:{path.name}" if node else f"{month}:{path.name}"
        sources[artifact_id] = {
            "artifact_id": artifact_id,
            "name": path.name,
            "kind": path.suffix.lstrip(".") or "file",
            "node": node or NO_NODE,
            "source": snapshot.relative_to(pdir).as_posix(),
            "sha256": _sha256_file(snapshot),
        }
    results = _build_month_results(args, pdir, batches)
    _persist_ingest(svc, args, pdir, results, sources, source_mode="file",
                    candidates=getattr(args, "_candidates", None))


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


def _build_llm_provider(args: argparse.Namespace):
    """构建 LLM 分类 Provider；测试通过替换本函数注入假客户端。"""
    from research_workflow.classification.llm import AnthropicClassificationProvider

    _load_dotenv(ROOT / ".env")
    return AnthropicClassificationProvider(model=args.model, batch_size=args.batch_size)


class _RowDetailProvider:
    """离线补齐：商品详情来自人工提供/修改的表格（表2 上传也走这里）。"""

    name = "file"

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.by_asin = {
            str(_col(row, "ASIN", "asin")).strip().upper(): row for row in rows if str(_col(row, "ASIN", "asin")).strip()
        }

    async def get_product_detail(self, asin: str, marketplace: str) -> Any:
        row = self.by_asin.get(asin.upper())
        if row is None:
            raise KeyError(asin)
        return row


def cmd_enrich(args: argparse.Namespace) -> None:
    """补齐商品卖点、卖家所属地等（表2）。live 走 SellerSprite asin_detail，离线读 --details-file。"""
    svc = _service(args)
    project = svc.store.get_project(args.project)
    if project is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)
    _require_approved(svc, args.project, pdir, ["data_review"])
    records = _load_records(pdir)
    asins = list(dict.fromkeys(r.asin for r in records))

    if args.live:
        _load_dotenv(ROOT / ".env")
        connector = SellerSpriteConnector(_build_gateway(ROOT / "config" / "mcp.json"))

        async def run_live():
            await connector.validate()
            return await ASINEnrichmentService(
                SellerSpriteProductDetailProvider(connector), concurrency=args.concurrency
            ).enrich(asins, project.marketplace)

        result = asyncio.run(run_live())
        source: dict[str, Any] = {"mode": "live", "provider": "sellersprite"}
    else:
        if not args.details_file:
            raise SystemExit(_emit({"error": "需要 --details-file（表2 明细）或 --live"}) or 1)
        snapshot = _snapshot_source(pdir, Path(args.details_file))
        provider = _RowDetailProvider(read_tabular_rows(snapshot))
        result = asyncio.run(ASINEnrichmentService(provider).enrich(asins, project.marketplace))
        source = {"mode": "file", "source": snapshot.relative_to(pdir).as_posix(), "sha256": _sha256_file(snapshot)}

    profiles = result.profiles
    missing = sorted(set(asins) - set(profiles))
    unknown_region_before = sum(1 for r in records if r.seller_region in {"", "UNKNOWN"})
    filled_region = len({r.asin for r in records
                         if r.seller_region in {"", "UNKNOWN"} and profiles.get(r.asin)
                         and profiles[r.asin].seller_region not in {"", "UNKNOWN"}})
    coverage = {
        "asins": len(asins), "profiles": len(profiles), "missing": len(missing),
        "with_bullets": sum(1 for p in profiles.values() if p.bullets),
        "seller_region_filled_asins": filled_region,
        "unknown_seller_region_rows_before": unknown_region_before,
    }
    _invalidate(svc, args.project, pdir, "enrich_asins")
    _save_json(pdir / "profiles.json", {
        "records_sha256": _sha256_file(pdir / "records.json"),
        "source": source, "coverage": coverage, "missing_asins": missing,
        "profiles": {asin: p.to_dict() for asin, p in sorted(profiles.items())},
    })
    svc.set_step(args.project, "enrich_asins", StepStatus.COMPLETED, output_ref="profiles.json")
    _emit({"project_id": args.project, "enrichment": "completed", "coverage": coverage,
           "missing_asins": missing[:50],
           "next": "classify（默认使用补齐后的卖点；缺失商品不会被当作已补齐）"})


def cmd_classify(args: argparse.Namespace) -> None:
    svc = _service(args)
    if svc.store.get_project(args.project) is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)
    _require_approved(svc, args.project, pdir, ["data_review"])
    records = _load_records(pdir)

    enriched = _valid_profiles(pdir)
    profiles: dict[str, ASINProfile] = {}
    for record in records:
        base = ASINProfile(
            asin=record.asin,
            title=record.title,
            brand=record.brand,
            seller_region=record.seller_region or "UNKNOWN",
            launch_date=record.launch_date,
            image_url=record.image_url,
        )
        extra = (enriched or {}).get(record.asin)
        if extra is not None:
            base = merge_profiles(extra, base)  # 补齐画像优先，缺失字段由 BSR 行兜底
        profiles.setdefault(record.asin, base)
    bullets_coverage = (
        sum(1 for p in profiles.values() if p.bullets) / len(profiles) * 100.0 if profiles else 0.0
    )

    labels = [
        ClassificationLabel(key=key, name=name)
        for key, name in _parse_kv(args.label, "--label").items()
    ]
    schema = ClassificationSchema(project_id=args.project, dimension=args.dimension, labels=labels)
    llm_meta: dict[str, Any] | None = None
    if args.provider == "llm":
        if enriched is None:
            raise ValueError("LLM 分类必须基于真实卖点：先运行 enrich 补齐商品信息")
        if bullets_coverage < args.min_bullets_coverage:
            raise ValueError(
                f"卖点覆盖率仅 {bullets_coverage:.0f}%（要求 ≥{args.min_bullets_coverage:.0f}%）；"
                "请补齐数据后重试，或显式降低 --min-bullets-coverage"
            )
        provider = _build_llm_provider(args)
    else:
        rules = {
            key: [word.strip() for word in value.split(",") if word.strip()]
            for key, value in _parse_kv(args.rule, "--rule").items()
        } if args.rule else {}
        provider = RuleClassificationProvider(rules)
    service = ClassificationService(provider)
    batch = asyncio.run(service.classify(schema, list(profiles.values())))
    if args.provider == "llm":
        usage = dataclasses.asdict(provider.usage)
        llm_meta = {"model": provider.model, "usage": usage}

    if enriched is not None:
        _invalidate(svc, args.project, pdir, "classify")
    else:
        _invalidate(svc, args.project, pdir, "enrich_asins")
        svc.set_step(args.project, "enrich_asins", StepStatus.SKIPPED)
    meta = {"provider": provider.name, "enrichment": "completed" if enriched is not None else "skipped",
            "bullets_coverage_pct": bullets_coverage, "llm": llm_meta}
    _save_json(pdir / "classification.json", {"schema": schema.canonical_payload(), "batch": batch.to_dict(), "meta": meta})
    svc.set_step(args.project, "classify", StepStatus.COMPLETED, output_ref="classification.json")
    checkpoint = svc.request_human_checkpoint(args.project, "classification_review",
        ("请检查 AI 分类结果（基于补齐后的商品卖点）；可修改后确认继续。" if args.provider == "llm"
         else "请检查规则分类结果；可修改后确认继续。当前未调用 LLM。")
        + ("" if enriched is not None else "未执行 ASIN 补全。"),
        {"schema": schema.canonical_payload(), "classification": batch.to_dict(), "meta": meta,
         "artifact_hashes": _hashes(pdir, ["classification.json", "profiles.json"])})

    counts: dict[str, int] = {}
    for row in batch.rows.values():
        counts[row.label] = counts.get(row.label, 0) + 1
    _emit(
        {
            "project_id": args.project,
            "classification_version": batch.version,
            "classification_provider": meta["provider"],
            "enrichment": meta["enrichment"],
            "bullets_coverage_pct": bullets_coverage,
            "llm": llm_meta,
            "label_counts": counts,
            "total_asins": len(batch.rows),
            "checkpoint_id": checkpoint.id,
            "checkpoint_step": "classification_review",
            "next": "resolve --approve(可用 --edit ASIN=标签 修改)后进入 analyze",
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


def cmd_nodes_discover(args: argparse.Namespace) -> None:
    """用产品关键词取样，统计类目节点分布，提议主节点/相关节点（须人工 nodes-confirm）。"""
    svc = _service(args)
    project = svc.store.get_project(args.project)
    if project is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)
    if args.live:
        keyword = args.keyword or project.primary_keyword
        if not keyword:
            raise ValueError("live 节点发现必须提供 --keyword 或项目 primary_keyword")
        _load_dotenv(ROOT / ".env")
        connector = SellerSpriteConnector(_build_gateway(ROOT / "config" / "mcp.json"))

        async def sample() -> list[dict[str, Any]]:
            await connector.validate()
            response = await connector.product_research(
                marketplace=project.marketplace, keyword=keyword, month=args.month,
                page_size=100, max_pages=args.max_pages)
            return response.data.get("items", []) if isinstance(response.data, dict) else []

        rows = asyncio.run(sample())
        origin: dict[str, Any] = {"mode": "live", "keyword": keyword, "month": args.month}
    else:
        if not args.file:
            raise SystemExit(_emit({"error": "需要 --file 样本文件，或使用 --live --keyword"}) or 1)
        path = Path(args.file)
        snapshot = _snapshot_source(pdir, path)
        rows = read_tabular_rows(snapshot)
        origin = {"mode": "file", "source": snapshot.relative_to(pdir).as_posix(),
                  "sha256": _sha256_file(snapshot)}
    proposal = discover_nodes(rows)
    if proposal.primary is None:
        raise SystemExit(_emit({
            "error": "样本中没有可识别的类目节点字段",
            "remedy": "对照原始快照确认节点字段名后补充 data/nodes.py 的 NODE_ID_ALIASES；或直接人工指定 nodes-confirm --allow-unlisted",
            "sampled_rows": proposal.sampled_rows}) or 1)
    _save_json(pdir / "nodes.json", {"origin": origin, "proposal": proposal.to_dict(), "confirmed": None})
    _emit({"project_id": args.project, "proposal": proposal.to_dict(),
           "next": "人工检查后运行 nodes-confirm --node <主节点> [--node <相关节点>...]"})


def cmd_nodes_confirm(args: argparse.Namespace) -> None:
    svc = _service(args)
    if svc.store.get_project(args.project) is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)
    path = pdir / "nodes.json"
    doc = _load_json(path) if path.exists() else {"origin": {"mode": "manual"}, "proposal": None, "confirmed": None}
    nodes = list(dict.fromkeys(n.strip() for n in args.node if n.strip()))
    if not nodes:
        raise ValueError("至少确认一个节点")
    primary = args.primary or nodes[0]
    if primary not in nodes:
        raise ValueError("--primary 必须在确认的节点内")
    proposal = doc.get("proposal")
    if proposal and not args.allow_unlisted:
        listed = {c["node_id"] for c in ([proposal["primary"]] if proposal["primary"] else []) + proposal["related"] + proposal["others"]}
        unknown = [n for n in nodes if n not in listed]
        if unknown:
            raise ValueError(f"节点不在发现结果内: {unknown}；确认是人工指定请加 --allow-unlisted")
    doc["confirmed"] = {"nodes": nodes, "primary": primary, "allow_unlisted": bool(args.allow_unlisted)}
    _save_json(path, doc)
    _emit({"project_id": args.project, "confirmed": doc["confirmed"],
           "next": "ingest --live --months ... （默认使用已确认节点；各节点按月分别取数后合并去重）"})


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
                promotion_threshold=args.promotion_threshold,
                promotion_window_months=args.promotion_window,
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
        "classification_provider": (envelope.get("meta") or {}).get("provider", "rules"),
        "analysis_provider": analysis.provider,
        "enrichment": (envelope.get("meta") or {}).get("enrichment", "skipped"),
        "classification_llm": (envelope.get("meta") or {}).get("llm"), "pairs": pairs, "periods": periods,
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
    missing_evidence: list[str] = ["当前采用规则分类与确定性模板解释，未调用 LLM", "当前 CLI 未执行 ASIN 补全", "研究路线确认仅登记，支线尚未自动执行"]
    if new_products is None:
        missing_evidence.append("未提供新品跟踪表(new_products),T09 新品结论缺失")

    output = Path(args.output)
    approvals = svc.store.get_state(args.project).get("approved_checkpoints", {})
    evidence_names = ["records.json", "quality.json", "sources.json", "classification.json", "analysis_context.json",
                      "market_metrics.json", "market_analysis.json", "competition.json", "new_products.json", "route_plan.json"]
    evidence_files = {name: pdir / name for name in evidence_names if (pdir / name).is_file()}
    evidence_files.update({source.sha256 + Path(source.source).suffix: (Path(source.source) if Path(source.source).is_absolute() else pdir / source.source) for source in sources})
    approval_history = {key: dataclasses.asdict(svc.store.get_checkpoint(value)) for key,value in approvals.items()}
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
            source_artifacts=[dataclasses.replace(source, source="evidence/" + source.sha256 + Path(source.source).suffix) for source in sources],
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
    p.add_argument("--no-metric-dedupe", action="store_true", help="关闭跨父体去重（销量+评分+评分数相同）")
    p.add_argument("--dedupe-title", action="store_true", help="启用旧口径：品牌+标题去重（可能误删，需人工抽查）")
    p.add_argument("--live", action="store_true", help="通过 MCP 直连 SellerSprite 取数")
    p.add_argument("--marketplace", help="live 模式站点;默认取项目站点")
    p.add_argument("--node", action="append", default=[],
                   help="类目节点；可多次。多个节点分别取数后在月内合并去重（已确认节点见 nodes confirm）")
    p.add_argument("--keyword", help="live 模式调研关键词")
    p.add_argument("--months", help="live 模式月份列表,如 2025-01,2026-01")
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--max-pages", type=int, default=20)
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("nodes-discover", help="按产品关键词发现类目节点并提议主节点（需人工确认）")
    p.add_argument("--project", required=True)
    p.add_argument("--file", help="取样结果文件（含节点字段）")
    p.add_argument("--live", action="store_true")
    p.add_argument("--keyword")
    p.add_argument("--month", help="取样月份 YYYY-MM；缺省由接口决定")
    p.add_argument("--max-pages", type=int, default=3)
    p.set_defaults(func=cmd_nodes_discover)

    p = sub.add_parser("nodes-confirm", help="人工确认研究使用的类目节点")
    p.add_argument("--project", required=True)
    p.add_argument("--node", action="append", default=[], required=True)
    p.add_argument("--primary")
    p.add_argument("--allow-unlisted", action="store_true", help="允许确认发现结果之外、由人工指定的节点")
    p.set_defaults(func=cmd_nodes_confirm)

    p = sub.add_parser("login", help="触发 SellerSprite MCP 登录流程(CDP Chrome)")
    p.add_argument("--wait", type=int, default=180)
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("classify", help="运行产品分类并请求人工复核")
    p.add_argument("--project", required=True)
    p.add_argument("--label", action="append", required=True, help="标签 key=中文名,可多次")
    p.add_argument("--dimension", default="segment")
    p.add_argument("--rule", action="append", default=[], help="规则 key=关键词1,关键词2,可多次")
    p.add_argument("--provider", choices=["rules", "llm"], default="rules",
                   help="rules=关键词规则；llm=Claude 分类（需先 enrich，密钥走 ANTHROPIC_API_KEY）")
    p.add_argument("--model", help="LLM 模型 ID；缺省读 RESEARCH_LLM_MODEL")
    p.add_argument("--batch-size", type=int, default=25)
    p.add_argument("--min-bullets-coverage", type=float, default=50.0,
                   help="LLM 分类要求的最低卖点覆盖率(%)")
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("enrich", help="补齐商品卖点/卖家所属地（表2）；支持 live 或人工明细文件")
    p.add_argument("--project", required=True)
    p.add_argument("--live", action="store_true")
    p.add_argument("--details-file", help="离线补齐明细（ASIN、标题、卖点、卖家所属地…），亦用于上传人工修改后的表2")
    p.add_argument("--concurrency", type=int, default=5)
    p.set_defaults(func=cmd_enrich)

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
    p.add_argument("--promotion-threshold", type=int, default=None,
                   help="推广阈值（人工设定的腰部销量）；不设则不计算推广率")
    p.add_argument("--promotion-window", type=int, default=3, help="推广窗口月数（含上架月）")
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
