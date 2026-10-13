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
import calendar
import dataclasses
import io
from contextlib import redirect_stdout
import json
import logging
import os
import re
import sqlite3
import tempfile
import sys
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

# 引擎根目录(config/、.env、runtime/ 所在):环境变量优先,否则取源码树的仓库根。
ROOT = Path(os.environ.get("PRODUCT_RESEARCH_ENGINE") or Path(__file__).resolve().parents[2])

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
from research_workflow.data.io import read_tabular_rows, write_csv_rows
from research_workflow.reconcile import flatten_metrics, reconcile, summarize, to_dicts
from research_workflow.data.models import BSRRecord, MonthlyBSRResult
from research_workflow.data.bsr_pipeline import normalize_seller_region, validate_month, _to_int
from research_workflow.connectors.base import ConnectorError
from research_workflow.connectors.sellersprite import SellerSpriteConnector
from research_workflow.domain import ProjectStatus, StepStatus
from research_workflow.domain.models import utc_now_iso
from research_workflow import external_ingest
from research_workflow.dossier import (
    ResearchDossierGenerator,
    ResearchDossierInput,
    SourceArtifact,
)
from research_workflow.dossier.external import external_gaps
from research_workflow.enrichment import (
    ASINEnrichmentService,
    SellerSpriteProductDetailProvider,
    enrich_bullets_unique_asins,
    enrich_missing_regions,
)
from research_workflow.enrichment.adapters import split_region_and_account_type
from research_workflow.enrichment.models import ASINProfile
from research_workflow.mcp import MCPGateway, MCPRegistry
from research_workflow.metrics import build_market_metrics
from research_workflow.metrics.models import MarketMetricsResult
from research_workflow.metrics import IterationConfig, IterationResult, build_iteration_comparison
from research_workflow.new_products.engine import _normalize_month
from research_workflow.new_products import (
    NewProductConfig,
    NewProductSnapshot,
    analyze_new_products,
    build_new_product_queue,
)
from research_workflow.new_products.models import NewProductAnalysisResult
from research_workflow.router import ResearchRouter
from research_workflow.router.models import ResearchRoutePlan
from research_workflow.store import SQLiteStore
from research_workflow.workflow import WorkflowService

logger = logging.getLogger("research_workflow.cli")
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
        # 不传 temperature/top_p/top_k:Sonnet 5/5.5、Opus 5.x 等当前模型拒绝非默认采样参数(400)。
        message = await client.messages.create(
            model=model, max_tokens=8192,
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
    if overlay:
        # 补齐/人工表2 只填 UNKNOWN 地区,不改人工审过的 records.json(其哈希已绑定 data_review)。
        machine = _load_json(pdir / "enrichment.json").get("regions", {}) if (pdir / "enrichment.json").is_file() else {}
        human = _table2_edits(pdir).get("regions", {})
        for record in records:
            if human.get(record.asin):  # 人工校正的表2 优先于一切(含源数据里已有的地区)
                record.seller_region = human[record.asin]
            elif record.seller_region in ("", "UNKNOWN") and machine.get(record.asin):
                record.seller_region = machine[record.asin]
    return records


def _table2_edits(pdir: Path) -> dict[str, Any]:
    path = pdir / "table2_edits.json"
    return _load_json(path) if path.is_file() else {}


def _parse_kv(items: list[str], what: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        key, _, value = item.partition("=")
        if not key.strip() or not value.strip():
            raise SystemExit(_emit({"error": f"非法 {what} 格式(应为 key=value): {item}"}) or 1)
        parsed[key.strip()] = value.strip()
    return parsed


STAGES = ["collect_bsr", "data_review", "enrich_asins", "enrichment_review", "classify", "classification_review",
          "apply_classification_review", "market_metrics", "market_analysis", "competition_metrics",
          "new_product_analysis", "research_route", "route_review", "research_route_review",
          "apply_route_review", "dossier"]
DERIVED_FILES = {"collect_bsr": ["table2_edits.json", "coverage.json", "table_edits.json"], "enrich_asins": ["enrichment.json"], "classify": ["classification.json"], "market_metrics": ["market_metrics.json",
                 "market_analysis.json", "competition.json", "new_products.json", "route_plan.json",
                 "analysis_context.json", "new_product_queue.json", "iteration.json"]}


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


HUMAN_FILES = {"table2_edits.json"}  # 含人工成果的派生文件:失效时移入 superseded/ 备份,不直接删除


def _invalidate(svc: WorkflowService, pid: str, pdir: Path, start: str) -> list[str]:
    """使 start 及其下游失效;返回被作废的人工成果文件名(已备份到 superseded/)。"""
    stages = STAGES[STAGES.index(start):]
    svc.invalidate_steps(pid, stages)
    superseded: list[str] = []
    for stage, names in DERIVED_FILES.items():
        if stage in stages:
            for name in names:
                path = pdir / name
                if name in HUMAN_FILES and path.is_file():
                    backup = pdir / "superseded" / f"{name}.{utc_now_iso().replace(':', '')}"
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    path.replace(backup)
                    superseded.append(name)
                else:
                    path.unlink(missing_ok=True)
    return superseded


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
                   if key.split(":", 1)[0] not in replaced_months and value.get("kind") != "human_table1"}
    all_sources.update(sources)
    if not results or any(not result.records for result in results):
        raise ValueError("每个导入月份必须有有效数据;空数据不能作为零销量市场进入分析")
    if any(result.quality.invalid_sales_rows for result in results):
        raise ValueError("存在缺失/非法销量;请修正源数据后重新导入，不能将缺失视为零销量")
    superseded = _invalidate(svc, args.project, pdir, "collect_bsr")

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
         "missing_reasons": coverage.get("missing_reasons", {}) if coverage else {},
         "all_months": sorted(by_month),
         "artifact_hashes": _hashes(pdir, ["records.json", "quality.json", "sources.json"]
                                    + (["coverage.json"] if coverage is not None else []))},
    )
    _emit(
        {
            "project_id": args.project,
            "source_mode": source_mode,
            "months": summary,
            "records_file": str(pdir / "records.json"),
            "all_months": sorted(by_month),
            "superseded_human_edits": superseded,
            **({"missing_months": sorted(coverage["missing"]),
                "missing_reasons": coverage.get("missing_reasons", {}),
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
    errored: dict[str, str] = {}
    partial_nodes: dict[str, list[str]] = {}
    raw_dir = pdir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for month in months:
        have = {scope: f for scope, f in fetched.items() if month in f.rows_by_month}
        reasons = {str(scope): f.missing_months[month] for scope, f in fetched.items() if month in f.missing_months}
        failed = sorted(scope for scope, reason in reasons.items() if reason.startswith("connector_error"))
        if failed:  # 接口失败≠无数据:不能当作缺月悄悄放过
            errored[month] = ", ".join(f"{scope}: {reasons[scope]}" for scope in failed)
            continue
        lacking = sorted(str(scope) for scope in fetched if scope not in have)
        if lacking:
            # 多节点月份必须各节点齐全:缺一个节点就等于把它的销量当 0,所以整月不纳入并显式报告
            missing[month] = (f"节点 {','.join(lacking)} 缺该月数据(多节点月份须各节点齐全,否则整月不纳入)"
                              if have else "; ".join(sorted(set(reasons.values())) or ["no data"]))
            if have:
                partial_nodes[month] = lacking
            continue
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

    if errored:
        logger.warning("live fetch connector errors months=%s", sorted(errored))
        raise ValueError(f"以下月份的接口调用失败(不是无数据),请检查会话/限流后重试: {errored}")
    if missing and not auto:
        raise ValueError(f"显式指定的月份无数据或节点不齐,不能静默跳过: {missing};请改月份/节点或使用 --months auto")
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
    by_month_files: dict[str, list[str]] = {}
    for file_name, result in zip(args.file, results):
        by_month_files.setdefault(result.month, []).append(Path(file_name).name)
    duplicated = {m: names for m, names in by_month_files.items() if len(names) > 1}
    if duplicated:
        raise ValueError(f"同一月份出现多个文件: {duplicated};多节点样本请用 --live --nodes 合并,或先在表格中合并为一个文件(静默覆盖会丢数据)")
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


def _needs_enrichment_review(pdir: Path) -> bool:
    """已有机器补齐或人工表2 修改时,下游必须先通过“补齐检查点”(表2 人工确认)。"""
    return (pdir / "enrichment.json").is_file() or (pdir / "table2_edits.json").is_file()


def _gate_steps(pdir: Path, steps: list[str]) -> list[str]:
    if _needs_enrichment_review(pdir) and "enrichment_review" not in steps:
        index = steps.index("data_review") + 1 if "data_review" in steps else 0
        return [*steps[:index], "enrichment_review", *steps[index:]]
    return steps


def _base_profiles(records: list[BSRRecord]) -> dict[str, ASINProfile]:
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
    return profiles


def _apply_enrichment(profiles: dict[str, ASINProfile], enrichment: dict[str, Any]) -> None:
    """把已落盘(且已人工确认)的机器补齐叠加到画像上;只填补,冲突/失败仍在审阅记录里。"""
    for asin, region in (enrichment.get("regions") or {}).items():
        if asin in profiles:
            profiles[asin].seller_region = region
    for asin, bullets in (enrichment.get("bullets") or {}).items():
        if asin in profiles:
            profiles[asin].bullets = list(bullets)


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


def cmd_enrich(args: argparse.Namespace) -> None:
    """独立的商品补齐步骤(表2:卖家所属地、卖点):补齐后必须经“补齐检查点”人工确认才能分类。"""
    svc = _service(args)
    project = svc.store.get_project(args.project)
    if project is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)
    _require_approved(svc, args.project, pdir, ["data_review"])
    records = _load_records(pdir, overlay=False)
    profiles = _base_profiles(records)
    enrichment = _run_enrichment(args, project, records, profiles)

    _invalidate(svc, args.project, pdir, "enrich_asins")  # 旧补齐、旧确认与下游分类全部作废
    _save_json(pdir / "enrichment.json", enrichment)
    svc.set_step(args.project, "enrich_asins", StepStatus.COMPLETED, output_ref="enrichment.json")
    table2 = _table2_edits(pdir)
    checkpoint = svc.request_human_checkpoint(
        args.project, "enrichment_review",
        "请检查商品补齐结果(表2:卖家所属地、卖点):覆盖情况、冲突与失败必须逐项核对。可 export-table --table 2 导出修改后 import-table 回传;确认后才能分类。",
        {"summary": enrichment["summary"], "conflicts": enrichment["conflicts"], "failures": enrichment["failures"],
         "human_table2_edits": bool(table2),
         "artifact_hashes": _hashes(pdir, ["enrichment.json"] + (["table2_edits.json"] if table2 else []))})
    _emit({"project_id": args.project, "enrichment": enrichment["summary"],
           "enrichment_conflicts": len(enrichment["conflicts"]), "enrichment_failures": len(enrichment["failures"]),
           "checkpoint_id": checkpoint.id, "checkpoint_step": "enrichment_review",
           "next": "resolve --approve 人工确认补齐结果后再 classify(冲突/失败不会被静默覆盖)"})


def cmd_classify(args: argparse.Namespace) -> None:
    svc = _service(args)
    project = svc.store.get_project(args.project)
    if project is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)
    if args.enrich:
        raise ValueError("补齐已拆为独立步骤:先运行 enrich 并在补齐检查点人工确认,再 classify(不再支持 classify --enrich)")
    _require_approved(svc, args.project, pdir, _gate_steps(pdir, ["data_review"]))
    records = _load_records(pdir, overlay=False)  # 用原始记录重算,避免叠加旧的补齐结果

    profiles = _base_profiles(records)
    llm_complete = _build_llm_complete() if args.provider == "llm" else None  # 先校验 LLM 配置,避免分类跑完才失败
    enrichment = _load_json(pdir / "enrichment.json") if (pdir / "enrichment.json").is_file() else None
    if enrichment is not None:
        _apply_enrichment(profiles, enrichment)  # 已人工确认的机器补齐
    table2 = _table2_edits(pdir)  # 人工校正的表2 优先于机器补齐
    for asin, bullets in table2.get("bullets", {}).items():
        if asin in profiles:
            profiles[asin].bullets = list(bullets)
    for asin, region in table2.get("regions", {}).items():
        if asin in profiles:
            profiles[asin].seller_region = region

    labels = [
        ClassificationLabel(key=key, name=name)
        for key, name in _parse_kv(args.label, "--label").items()
    ]
    schema = ClassificationSchema(project_id=args.project, dimension=args.dimension, labels=labels)
    if args.provider == "llm":
        provider = LLMClassificationProvider(llm_complete)
    else:
        rules = {
            key: [word.strip() for word in value.split(",") if word.strip()]
            for key, value in _parse_kv(args.rule, "--rule").items()
        } if args.rule else {}
        provider = RuleClassificationProvider(rules)
    batch = asyncio.run(ClassificationService(provider).classify(schema, list(profiles.values())))
    if args.provider == "llm":
        failed = [row for row in batch.rows.values() if row.review_reason.startswith("llm call failed")]
        if batch.rows and len(failed) == len(batch.rows):  # 全部调用失败≠"全部待人工":必须让命令失败,不保存任何结果
            logger.error("LLM classification failed for all %d ASINs: %s", len(failed), failed[0].review_reason[:200])
            raise ValueError(f"LLM 分类调用全部失败({len(failed)} 个 ASIN),未保存任何结果;"
                             f"请检查 API 密钥/模型名/网络。原因示例: {failed[0].review_reason[:200]}")

    if _needs_enrichment_review(pdir):
        _invalidate(svc, args.project, pdir, "classify")  # 补齐结果与其人工确认保持有效,只重做分类
    else:
        _invalidate(svc, args.project, pdir, "enrich_asins")
        svc.set_step(args.project, "enrich_asins", StepStatus.SKIPPED)
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
         "artifact_hashes": _hashes(pdir, ["classification.json"] + (["enrichment.json"] if enrichment else [])
                                    + (["table2_edits.json"] if table2 else []))})

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


def status_payload(svc: WorkflowService, root: str | Path, project_id: str) -> dict[str, Any]:
    """项目现场:状态快照、待裁决检查点、已有工件、下一步(CLI 与 Web 共用,只读)。"""
    snapshot = svc.snapshot(project_id)
    pdir = Path(root) / project_id
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
    elif _needs_enrichment_review(pdir) and "enrichment_review" not in completed:
        next_action = "enrich"  # 补齐结果尚未人工确认(被驳回或被作废):重新补齐,或 import-table 回传表2
    elif "classification_review" not in completed:
        next_action = "classify"
    elif "research_route_review" not in completed:
        next_action = "analyze"
    elif "dossier" not in completed:
        next_action = "dossier"
    else:
        next_action = "done"
    return {**snapshot, "pending_checkpoint": pending, "artifacts": artifacts, "next": next_action}


def cmd_status(args: argparse.Namespace) -> None:
    _emit(status_payload(_service(args), args.root, args.project))


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
        "enrichment_review": "classify",
        "classification_review": "analyze",
        "route_review": "dossier",
        "research_route_review": "dossier",
    }.get(checkpoint.step_key, "status")
    if not args.approve:
        next_action = {"data_review": "ingest", "enrichment_review": "enrich", "classification_review": "classify"}.get(checkpoint.step_key, "analyze")
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
    _require_approved(svc, args.project, pdir, _gate_steps(pdir, ["data_review", "classification_review"]))
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
    new_product_queue = None
    queue_excluded = 0
    queue_filter = "no_new_products_file"
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
                parent_asin=str(_col(row, "父ASIN", "parent_asin")).strip().upper(),
            )
            for row in rows
            if str(_col(row, "ASIN", "asin")).strip()
        ]
        queue_month = args.study_month or max(_normalize_month(snapshot.month) for snapshot in snapshots)
        new_product_queue = build_new_product_queue(snapshots, study_month=queue_month)
        analysis_snapshots = snapshots
        if args.skip_queue_filter:
            queue_filter = "skipped_by_flag"
        elif not any(snapshot.launch_date for snapshot in snapshots):
            queue_filter = "skipped_no_launch_dates"  # 整表没有上架时间,无法判断新品窗口/老带新:显式降级并披露
        else:
            queue_filter = "applied"
        if queue_filter == "applied":
            # 留存率等指标的分母只统计通过"新品窗口 / 老带新 / 人工排除"过滤的 ASIN(口径确认单 #4)
            in_queue = {item.asin for item in new_product_queue.items}
            analysis_snapshots = [snapshot for snapshot in snapshots if snapshot.asin in in_queue]
            queue_excluded = len({s.asin for s in snapshots}) - len(in_queue & {s.asin for s in snapshots})
            if not analysis_snapshots:
                raise ValueError("新品队列为空:没有 ASIN 通过新品窗口/老带新/人工排除过滤;"
                                 "若新品表已人工预筛,请加 --skip-queue-filter")
        new_products = analyze_new_products(
            analysis_snapshots,
            NewProductConfig(
                sales_threshold=args.sales_threshold,
                consecutive_months=args.consecutive_months,
                early_threshold_window_months=args.early_window,
                current_survival_threshold=args.survival_threshold,
                promotion_waist_sales_threshold=args.promotion_threshold,
                retention_sales_threshold=args.retention_threshold,
                study_month=args.study_month,
            ),
        )

    route_plan = ResearchRouter().suggest(market, competition, new_products)
    last_month = max(record.month for record in records)  # 取数据最大月份的月末,同输入同输出(不依赖今天的日期)
    as_of = date(int(last_month[:4]), int(last_month[5:7]), 1)
    as_of = as_of.replace(day=calendar.monthrange(as_of.year, as_of.month)[1])
    iteration = build_iteration_comparison(records, classifications, config=IterationConfig(as_of=as_of))

    _invalidate(svc, args.project, pdir, "market_metrics")
    _save_json(pdir / "market_metrics.json", market.to_dict())
    _save_json(pdir / "market_analysis.json", analysis.to_dict())
    _save_json(pdir / "competition.json", competition.to_dict())
    if new_products is not None:
        _save_json(pdir / "new_products.json", new_products.to_dict())
        _save_json(pdir / "new_product_queue.json", new_product_queue.to_dict())
    _save_json(pdir / "iteration.json", iteration.to_dict())
    _save_json(pdir / "route_plan.json", route_plan.to_dict())
    _save_json(pdir / "analysis_context.json", {
        "classification_version": envelope["batch"]["version"],
        "classification_provider": envelope.get("provider", "rules"), "analysis_provider": analysis.provider,
        "enrichment": "done" if (pdir / "enrichment.json").is_file() else "skipped",
        "new_products_excluded_by_queue": queue_excluded,
        "queue_filter": queue_filter,
        "multi_node": len((_load_json(pdir / "coverage.json").get("nodes") or [])) > 1 if (pdir / "coverage.json").is_file() else False, "pairs": pairs, "periods": periods,
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
            "market_analysis.json", "competition.json", "new_products.json", "route_plan.json", "analysis_context.json",
            "new_product_queue.json", "iteration.json"])})

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
            "checkpoint_step": "research_route_review",
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
    _require_approved(svc, args.project, pdir, _gate_steps(pdir, ["data_review", "classification_review", "research_route_review"]))
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
    if context.get("queue_filter") == "skipped_no_launch_dates":
        missing_evidence.append("新品表缺少上架时间:未做新品窗口/老带新过滤,留存率分母含全部输入 ASIN")
    elif context.get("queue_filter") == "skipped_by_flag":
        missing_evidence.append("已按用户要求跳过新品队列过滤(新品表须已人工预筛)")
    if context.get("multi_node"):
        missing_evidence.append("多节点合并样本:累计 BSR 层级/头腰尾/虹吸按各节点源 BSR 名次排序,跨节点名次不可直接比较;"
                                "请以销量排序的单点名次矩阵为准")
    if new_products is None:
        missing_evidence.append("未提供新品跟踪表(new_products),T09 新品结论缺失")

    # 外部证据(证据登记,非检查点):导出前校验 index.json 登记的哈希,篡改/过期即报错
    external = external_ingest.load_external(pdir) or external_ingest.empty_external()
    missing_evidence.extend(external_gaps(external))

    output = Path(args.output)
    approvals = svc.store.get_state(args.project).get("approved_checkpoints", {})
    evidence_names = ["records.json", "quality.json", "sources.json", "classification.json", "analysis_context.json",
                      "market_metrics.json", "market_analysis.json", "competition.json", "new_products.json", "route_plan.json",
                      "new_product_queue.json", "iteration.json", "enrichment.json", "coverage.json",
                      "table2_edits.json", "table_edits.json"]
    evidence_files = {name: pdir / name for name in evidence_names if (pdir / name).is_file()}
    evidence_files.update({source.sha256 + Path(source.source).suffix: (Path(source.source) if Path(source.source).is_absolute() else pdir / source.source) for source in sources})
    evidence_files.update(external_ingest.evidence_files(pdir))
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
            iteration=(rebuild(IterationResult, _load_json(pdir / "iteration.json"))
                       if (pdir / "iteration.json").is_file() else None),
            records=_load_records(pdir),
            classification=rebuild(ClassificationBatch, classification_envelope["batch"]),
            data_version=f"records.json@{records_hash[:12]}",
            data_hash=records_hash,
            approval_history=workbook_approvals,
            data_quality=data_quality,
            missing_evidence=missing_evidence,
            external=external,
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


# --- 外部证据登记与预测(证据登记不是人工检查点;external/ 不参与已审工件哈希) ---

def cmd_import_evidence(args: argparse.Namespace) -> None:
    svc = _service(args)
    if svc.store.get_project(args.project) is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)
    path = Path(args.file)
    meta = _parse_kv(args.meta, "--meta") if args.meta else {}
    # 先完整解析校验(库抛的 ValueError 原样上抛),通过后才快照与落盘:失败不改任何状态
    entry = external_ingest.prepare_import(pdir, args.kind, path, meta, utc_now_iso())
    snapshot = _snapshot_source(pdir, path)
    if _sha256_file(snapshot) != entry["sha256"]:
        raise ValueError("导入过程中源文件发生变化;请重新导入")
    row = external_ingest.commit_import(pdir, args.kind, entry, snapshot)
    _emit({
        "project_id": args.project, "kind": args.kind, "seq": row["seq"], "file": row["file_name"],
        "sha256": row["sha256"], "snapshot": row["snapshot"], "records": row["record_count"],
        "summary": row["summary"], "index": str(pdir / external_ingest.EXTERNAL_DIR / external_ingest.INDEX_NAME),
        "note": "证据登记,不是人工检查点;不影响已审批工件,导出 dossier 时随 evidence/ 与 manifest 一并校验",
        "next": "继续 import-evidence,或 build-market-forecast / build-cross-channel,或 dossier",
    })


def cmd_build_market_forecast(args: argparse.Namespace) -> None:
    svc = _service(args)
    if svc.store.get_project(args.project) is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)
    doc = external_ingest.build_forecast(
        pdir, scope=args.scope, method=args.method, horizon_years=args.horizon_years,
        granularity=args.granularity, coverage_ratio=args.coverage_ratio, coverage_source=args.coverage_source,
        target_scope=args.target_scope, low_growth=args.low_growth, high_growth=args.high_growth,
        today=_today(), created_at=utc_now_iso(),
    )
    result = doc["result"]
    _emit({
        "project_id": args.project, "is_forecast": True, "scope": result["scope"], "source_scope": result["source_scope"],
        "method": result["method"], "base_period": result["base_period"], "base_value": result["base_value"],
        "horizon": result["horizon"], "scenarios": sorted(result["scenarios"]),
        "assumptions": result["assumptions"], "file": str(pdir / external_ingest.EXTERNAL_DIR / external_ingest.FORECAST_NAME),
        "note": "预测值是外推,不是观测;假设见 assumptions。证据登记,不是人工检查点",
        "next": "可选 build-cross-channel(需已导入 tiktok-units),或 dossier",
    })


def cmd_build_cross_channel(args: argparse.Namespace) -> None:
    svc = _service(args)
    if svc.store.get_project(args.project) is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)
    doc = external_ingest.build_cross_channel(pdir, geo=args.geo, segment=args.segment, created_at=utc_now_iso())
    summary = doc["summary"]
    _emit({
        "project_id": args.project, "geo": doc["geo"], "segment": doc["segment"], "unit": summary["unit"],
        "rows": len(summary["table"]), "limitations": summary["limitations"],
        "human_decision_slot": summary["human_decision_slot"],
        "file": str(pdir / external_ingest.EXTERNAL_DIR / external_ingest.CROSS_CHANNEL_NAME),
        "note": "仅事实整合,不含商业建议;决策栏由人工填写。证据登记,不是人工检查点",
        "next": "dossier",
    })


# --- 三张表的人工校正协议(T1-9):导出 → 修改 → 上传校验 → 旧审批失效 → 重新确认 ---

TABLE1_COLUMNS = ["month", "asin", "brand", "title", "monthly_sales", "node_rank", "sales_rank", "seller_region",
                  "launch_date", "parent_asin", "seller_id", "rating", "rating_count"]
# 只读列:sales_rank 由系统按销量重算;parent_asin/rating/rating_count 决定去重,改它们必须重新 ingest 才能重新去重
TABLE1_READONLY = {"month", "asin", "sales_rank", "parent_asin", "rating", "rating_count"}
TABLE1_EDITABLE = [c for c in TABLE1_COLUMNS if c not in TABLE1_READONLY]
TABLE2_COLUMNS = ["asin", "title", "brand", "seller_region", "bullets"]
TABLE3_COLUMNS = ["asin", "label", "confidence", "evidence", "needs_review", "review_reason"]
BULLET_SEP = "||"


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _read_table(path: str, required: list[str], allowed: list[str]) -> list[dict[str, str]]:
    file = Path(path)
    if not file.is_file():
        raise ValueError(f"文件不存在: {path}")
    rows = [{str(k).strip(): _cell(v) for k, v in row.items() if k is not None} for row in read_tabular_rows(file)]
    if not rows:
        raise ValueError("上传的表格为空;若要删除全部数据请重新 ingest")
    columns = set(rows[0])
    missing = [c for c in required if c not in columns]
    if missing:
        raise ValueError(f"缺少必需列: {missing}")
    unknown = sorted(columns - set(allowed))
    if unknown:
        raise ValueError(f"含未知列(请使用导出表的列,不要增删列): {unknown}")
    return rows


def _asin_views(pdir: Path) -> tuple[dict[str, BSRRecord], dict[str, list[str]]]:
    """表2 的当前视图:每个 ASIN 取最新月份记录(地区已叠加补齐/人工),卖点取人工 > 机器补齐。"""
    latest: dict[str, BSRRecord] = {}
    for record in _load_records(pdir):
        if record.asin not in latest or record.month >= latest[record.asin].month:
            latest[record.asin] = record
    bullets: dict[str, list[str]] = {}
    if (pdir / "enrichment.json").is_file():
        bullets.update({a: list(v) for a, v in _load_json(pdir / "enrichment.json").get("bullets", {}).items()})
    bullets.update({a: list(v) for a, v in _table2_edits(pdir).get("bullets", {}).items()})
    return latest, bullets


def cmd_export_table(args: argparse.Namespace) -> None:
    svc = _service(args)
    if svc.store.get_project(args.project) is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)
    if args.table == 1:
        rows = [{c: getattr(r, c) for c in TABLE1_COLUMNS} for r in _load_records(pdir, overlay=False)]
    elif args.table == 2:
        latest, bullets = _asin_views(pdir)
        rows = [{"asin": a, "title": r.title, "brand": r.brand, "seller_region": r.seller_region,
                 "bullets": f" {BULLET_SEP} ".join(bullets.get(a, []))} for a, r in sorted(latest.items())]
    else:
        envelope = _require_file(pdir / "classification.json", "尚无分类结果;先运行 classify")
        rows = [{"asin": a, **{c: row.get(c, "") for c in TABLE3_COLUMNS[1:]}}
                for a, row in sorted(envelope["batch"]["rows"].items())]
    write_csv_rows(args.output, rows)
    _emit({"project_id": args.project, "table": args.table, "rows": len(rows), "file": str(Path(args.output).resolve()),
           "next": "在表格软件中修改后,用 import-table 上传;修改后旧审批失效,需重新人工确认"})


def _parse_table1(rows: list[dict[str, str]], current: list[BSRRecord]) -> tuple[list[BSRRecord], int, int]:
    index = {(r.month, r.asin): r for r in current}
    seen: set[tuple[str, str]] = set()
    updated: list[BSRRecord] = []
    changed = 0
    for number, row in enumerate(rows, start=2):
        key = (row["month"], row["asin"].upper())
        if key not in index:
            raise ValueError(f"第 {number} 行 {key} 不存在于当前表1;不能新增行(请重新 ingest)")
        if key in seen:
            raise ValueError(f"第 {number} 行 {key} 重复")
        seen.add(key)
        sales = _to_int(row.get("monthly_sales"), None) if "monthly_sales" in row else None
        if sales is None:
            raise ValueError(f"第 {number} 行销量必须是非负整数,缺失不能当作 0")
        base = index[key]
        edited = dataclasses.replace(base, monthly_sales=sales)
        for column in TABLE1_EDITABLE:
            if column == "monthly_sales" or column not in row:
                continue
            text = row[column]
            if column in {"node_rank", "sales_rank", "rating_count"}:
                value: Any = _to_int(text, None) if text else None
                if text and value is None:
                    raise ValueError(f"第 {number} 行 {column} 必须是非负整数")
            elif column == "rating":
                try:
                    value = float(text) if text else None
                except ValueError as exc:
                    raise ValueError(f"第 {number} 行 rating 必须是数字") from exc
            elif column == "seller_region":
                region, account_type = split_region_and_account_type(text)
                if account_type:
                    raise ValueError(f"第 {number} 行 {text} 是账号类型而非地理国家,请填写卖家所属国家/地区")
                value = region
            elif column in {"launch_date"}:
                value = text or None
            else:
                value = text
            edited = dataclasses.replace(edited, **{column: value})
        changed += sum(getattr(edited, c) != getattr(base, c) for c in TABLE1_EDITABLE)
        updated.append(edited)
    # 删除后每个月至少保留 1 行:空月份不能当作零销量市场
    kept_months = {r.month for r in updated}
    empty = sorted({r.month for r in current} - kept_months)
    if empty:
        raise ValueError(f"以下月份被全部删除,空数据不能作为零销量市场: {empty}")
    return updated, changed, len(current) - len(updated)


def _rerank(records: list[BSRRecord]) -> list[BSRRecord]:
    by_month: dict[str, list[BSRRecord]] = {}
    for record in records:
        by_month.setdefault(record.month, []).append(record)
    out: list[BSRRecord] = []
    for month in sorted(by_month):
        ordered = sorted(by_month[month], key=lambda r: (-r.monthly_sales,
                         r.node_rank if r.node_rank is not None else 10**9, r.asin))
        out.extend(dataclasses.replace(r, sales_rank=i) for i, r in enumerate(ordered, start=1))
    return out


def _import_table1(svc: WorkflowService, args: argparse.Namespace, pdir: Path) -> None:
    rows = _read_table(args.file, ["month", "asin", "monthly_sales"], TABLE1_COLUMNS)
    for row in rows:
        row["month"] = validate_month(row["month"])
    current = _load_records(pdir, overlay=False)
    updated, changed, deleted = _parse_table1(rows, current)
    if changed == 0 and deleted == 0:
        raise ValueError("上传的表1与当前内容完全相同;没有需要重新审批的修改")
    updated = _rerank(updated)
    snapshot = _snapshot_source(pdir, Path(args.file))
    previous_hash = _sha256_file(pdir / "records.json")

    coverage = _load_json(pdir / "coverage.json") if (pdir / "coverage.json").is_file() else None
    edits_log = _load_json(pdir / "table_edits.json") if (pdir / "table_edits.json").is_file() else []
    superseded = _invalidate(svc, args.project, pdir, "collect_bsr")
    if coverage is not None:  # 缺月信息仍然适用于修改后的数据
        _save_json(pdir / "coverage.json", coverage)
    by_month: dict[str, list[dict[str, Any]]] = {}
    for record in updated:
        by_month.setdefault(record.month, []).append(record.to_dict())
    _save_json(pdir / "records.json", by_month)
    # 质量统计随人工修改重算,并标注"人工修改后",避免资料包沿用修改前的行数
    quality = _load_json(pdir / "quality.json")
    for month, month_rows in by_month.items():
        entry = quality.setdefault(month, {"stats": {}, "quality": {}})
        entry["stats"]["output_rows"] = len(month_rows)
        entry["quality"].update({
            "missing_title_rows": sum(1 for r in month_rows if not str(r.get("title") or "").strip()),
            "missing_brand_rows": sum(1 for r in month_rows if not str(r.get("brand") or "").strip()),
            "unknown_seller_region_rows": sum(1 for r in month_rows if r.get("seller_region") in ("", "UNKNOWN")),
            "missing_rank_rows": sum(1 for r in month_rows if r.get("node_rank") is None),
        })
        entry["human_edited"] = True
    _save_json(pdir / "quality.json", quality)
    sources = _load_json(pdir / "sources.json")
    artifact_id = f"table1-edit:{_sha256_file(snapshot)[:12]}"
    sources[artifact_id] = {"artifact_id": artifact_id, "name": f"人工校正表1 {Path(args.file).name}", "kind": "human_table1",
                            "source": snapshot.relative_to(pdir).as_posix(), "sha256": _sha256_file(snapshot)}
    _save_json(pdir / "sources.json", sources)
    edits_log.append({"table": 1, "file_sha256": _sha256_file(snapshot), "changed_cells": changed,
                      "deleted_rows": deleted, "previous_records_sha256": previous_hash, "edited_at": utc_now_iso()})
    _save_json(pdir / "table_edits.json", edits_log)

    svc.set_step(args.project, "collect_bsr", StepStatus.COMPLETED, output_ref="records.json")
    summary = {m: {"output_rows": len(v)} for m, v in sorted(by_month.items())}
    checkpoint = svc.request_human_checkpoint(
        args.project, "data_review",
        "表1 已被人工修改并重新上传,请复核修改后的月度数据;确认前下游不可继续。",
        {"months": summary, "sources": list(sources.values()), "source_mode": "human_table1",
         "table_edit": edits_log[-1],
         "missing_months": sorted(coverage["missing"]) if coverage else [],
         "missing_reasons": coverage.get("missing_reasons", {}) if coverage else {},
         "superseded_human_edits": superseded,
         "artifact_hashes": _hashes(pdir, ["records.json", "quality.json", "sources.json", "table_edits.json"]
                                    + (["coverage.json"] if coverage is not None else []))})
    _emit({"project_id": args.project, "table": 1, "changed_cells": changed, "deleted_rows": deleted,
           "superseded_human_edits": superseded,
           "checkpoint_id": checkpoint.id, "checkpoint_step": "data_review",
           "next": "resolve --approve 重新确认表1 后再 classify"})


def _import_table2(svc: WorkflowService, args: argparse.Namespace, pdir: Path) -> None:
    rows = _read_table(args.file, ["asin"], TABLE2_COLUMNS)
    latest, bullets = _asin_views(pdir)
    edits = _table2_edits(pdir)
    regions: dict[str, str] = dict(edits.get("regions", {}))
    bullet_edits: dict[str, list[str]] = {a: list(v) for a, v in edits.get("bullets", {}).items()}
    changed = 0
    for number, row in enumerate(rows, start=2):
        asin = row["asin"].upper()
        if asin not in latest:
            raise ValueError(f"第 {number} 行 ASIN {asin} 不存在于表1")
        if "seller_region" in row and row["seller_region"]:
            region, account_type = split_region_and_account_type(row["seller_region"])
            if account_type:
                raise ValueError(f"第 {number} 行 {row['seller_region']} 是账号类型而非地理国家,请填写卖家所属国家/地区")
            if region != "UNKNOWN":
                changed += region != latest[asin].seller_region
                regions[asin] = region
        if "bullets" in row:
            parsed = [b.strip() for b in row["bullets"].split(BULLET_SEP) if b.strip()]
            current_bullets = bullets.get(asin, [])
            changed += parsed != current_bullets
            if parsed or current_bullets:  # 清空也是显式的人工覆盖([]),机器补齐的卖点不会再回来
                bullet_edits[asin] = parsed
    if changed == 0:
        raise ValueError("上传的表2与当前内容完全相同;没有需要重新审批的修改")
    snapshot = _snapshot_source(pdir, Path(args.file))
    _invalidate(svc, args.project, pdir, "enrichment_review")  # 保留机器补齐,作废其确认与下游分类
    _save_json(pdir / "table2_edits.json", {"regions": regions, "bullets": bullet_edits,
                                            "file_sha256": _sha256_file(snapshot), "edited_at": utc_now_iso()})
    machine = _load_json(pdir / "enrichment.json") if (pdir / "enrichment.json").is_file() else None
    svc.set_step(args.project, "enrich_asins", StepStatus.COMPLETED if machine else StepStatus.SKIPPED)
    checkpoint = svc.request_human_checkpoint(
        args.project, "enrichment_review",
        "表2 已被人工修改并重新上传,请复核补齐结果;确认前不能分类。",
        {"summary": machine["summary"] if machine else None, "conflicts": machine["conflicts"] if machine else [],
         "failures": machine["failures"] if machine else [], "human_table2_edits": True,
         "table_edit": {"table": 2, "changed_cells": changed, "file_sha256": _sha256_file(snapshot)},
         "artifact_hashes": _hashes(pdir, ["table2_edits.json"] + (["enrichment.json"] if machine else []))})
    _emit({"project_id": args.project, "table": 2, "changed_cells": changed,
           "checkpoint_id": checkpoint.id, "checkpoint_step": "enrichment_review",
           "next": "resolve --approve 重新确认补齐检查点后再 classify(表2 的人工修改会被采用)"})


def _import_table3(svc: WorkflowService, args: argparse.Namespace, pdir: Path) -> None:
    rows = _read_table(args.file, ["asin", "label"], TABLE3_COLUMNS)
    envelope = _require_file(pdir / "classification.json", "尚无分类结果;先运行 classify")
    schema = rebuild(ClassificationSchema, envelope["schema"])
    batch = rebuild(ClassificationBatch, envelope["batch"])
    uploaded = {row["asin"].upper(): row["label"] for row in rows}
    if len(uploaded) != len(rows):
        raise ValueError("表3 含重复 ASIN")
    if set(uploaded) != set(batch.rows):
        raise ValueError(f"表3 的 ASIN 集合必须与当前分类一致(缺少 {sorted(set(batch.rows) - set(uploaded))},"
                         f"多出 {sorted(set(uploaded) - set(batch.rows))})")
    invalid = sorted({label for label in uploaded.values() if label not in schema.allowed_labels})
    if invalid:
        raise ValueError(f"含不在标签体系内的标签: {invalid};允许: {sorted(schema.allowed_labels)}")
    edits = {asin: label for asin, label in uploaded.items() if label != batch.rows[asin].label}
    if not edits:
        raise ValueError("上传的表3与当前分类完全相同;没有需要重新审批的修改")
    snapshot = _snapshot_source(pdir, Path(args.file))
    edited = ClassificationService(RuleClassificationProvider({})).apply_human_edits(schema, batch, edits)
    edited = dataclasses.replace(edited, approved=False)  # 批准只能由人在检查点上做出

    _invalidate(svc, args.project, pdir, "classification_review")
    _save_json(pdir / "classification.json", {**envelope, "batch": edited.to_dict(), "human_edited": True,
                                              "table_edit_sha256": _sha256_file(snapshot)})
    needs_review = sorted(a for a, row in edited.rows.items() if row.needs_review)
    checkpoint = svc.request_human_checkpoint(
        args.project, "classification_review",
        "表3 已被人工修改并重新上传,请复核分类;确认前下游不可继续。",
        {"schema": schema.canonical_payload(), "classification": edited.to_dict(), "needs_review": needs_review,
         "table_edit": {"table": 3, "changed_labels": len(edits), "file_sha256": _sha256_file(snapshot)},
         "artifact_hashes": _hashes(pdir, ["classification.json"] + [n for n in ("enrichment.json", "table2_edits.json")
                                                                      if (pdir / n).is_file()])})
    _emit({"project_id": args.project, "table": 3, "changed_labels": len(edits), "needs_review": needs_review,
           "checkpoint_id": checkpoint.id, "checkpoint_step": "classification_review",
           "next": "resolve --approve 重新确认分类后再 analyze"})


def cmd_import_table(args: argparse.Namespace) -> None:
    svc = _service(args)
    if svc.store.get_project(args.project) is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)
    {1: _import_table1, 2: _import_table2, 3: _import_table3}[args.table](svc, args, pdir)


def cmd_reconcile(args: argparse.Namespace) -> None:
    """把已计算的指标与人工结果逐项对账,输出差异报告(验收用,只读)。"""
    svc = _service(args)
    if svc.store.get_project(args.project) is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)
    market = _require_file(pdir / "market_metrics.json", "尚未分析;先运行 analyze")
    competition = _load_json(pdir / "competition.json") if (pdir / "competition.json").exists() else None
    new_products = _load_json(pdir / "new_products.json") if (pdir / "new_products.json").exists() else None
    flat = flatten_metrics(market, competition, new_products)
    ref = Path(args.reference)
    if not ref.exists():
        raise KeyError(f"参考表不存在: {args.reference}")
    rows = reconcile(read_tabular_rows(ref), flat)
    report = to_dicts(rows)
    if args.output:
        write_csv_rows(Path(args.output), report)
    bad = [r for r in report if r["status"] != "MATCH"]
    _emit({"project_id": args.project, "summary": summarize(rows), "mismatches": bad[:100],
           "file": str(Path(args.output).resolve()) if args.output else None,
           "available_keys_sample": sorted(flat)[:20] if any(r["status"] == "MISSING" for r in report) else None,
           "next": "全部 MATCH 才算该批对账通过;MISMATCH/MISSING 须逐项查明原因(口径差异/数据缺失/缺陷),不得为凑数改容差"})


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

    p = sub.add_parser("enrich", help="商品补齐(表2:卖家所属地、卖点;需已登录 SellerSprite),完成后须在补齐检查点人工确认")
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("classify", help="运行产品分类并请求人工复核")
    p.add_argument("--project", required=True)
    p.add_argument("--label", action="append", required=True, help="标签 key=中文名,可多次")
    p.add_argument("--dimension", default="segment")
    p.add_argument("--rule", action="append", default=[], help="规则 key=关键词1,关键词2,可多次")
    p.add_argument("--provider", choices=["rules", "llm"], default="rules",
                   help="分类提供者:rules=关键词规则(默认);llm=大模型(需 ANTHROPIC_API_KEY,歧义标待人工)")
    p.add_argument("--enrich", action="store_true", help=argparse.SUPPRESS)  # 已废弃:请用独立的 enrich 命令
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("export-table", help="导出表1(清洗月度数据)/表2(唯一 ASIN 卖点地区)/表3(分类结果)供人工修改")
    p.add_argument("--project", required=True)
    p.add_argument("--table", type=int, choices=[1, 2, 3], required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_export_table)

    p = sub.add_parser("import-table", help="上传人工修改后的表;校验通过后旧审批失效并重新请求人工确认")
    p.add_argument("--project", required=True)
    p.add_argument("--table", type=int, choices=[1, 2, 3], required=True)
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_import_table)

    p = sub.add_parser("reconcile", help="指标与人工结果逐项对账(验收,只读):参考表 key,expected[,tolerance_abs,tolerance_pct]")
    p.add_argument("--project", required=True)
    p.add_argument("--reference", required=True)
    p.add_argument("--output")
    p.set_defaults(func=cmd_reconcile)

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
                   help="推起成功的人工腰部销量阈值(默认回退 --sales-threshold,须人工确认)")
    p.add_argument("--retention-threshold", type=int, default=None,
                   help="留存达标销量阈值(默认回退 --survival-threshold/--sales-threshold,须人工确认)")
    p.add_argument("--study-month", help="研究当月 YYYY-MM;提供时校验新品输入最大月份必须一致")
    p.add_argument("--skip-queue-filter", action="store_true",
                   help="新品表已人工预筛时跳过新品队列过滤(默认:只统计通过新品窗口/老带新/人工排除过滤的 ASIN)")
    p.add_argument("--pair", action="append", default=[], help="份额分解对 YYYY-MM:YYYY-MM")
    p.add_argument("--period", action="append", default=[], help="跨期比较 a起点:a终点:b起点:b终点")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("import-evidence", help="登记外部证据(趋势/研报/TikTok 台数/专利/广告/大品牌/站外);证据登记不是人工检查点")
    p.add_argument("--project", required=True)
    p.add_argument("--kind", required=True, choices=list(external_ingest.KINDS))
    p.add_argument("--file", required=True)
    p.add_argument("--meta", action="append", default=[], help="来源/口径元数据 key=value,可多次(trends 需 geo/timeframe/source_name/source_url/as_of)")
    p.set_defaults(func=cmd_import_evidence)

    p = sub.add_parser("build-market-forecast", help="analyze 之后:对 Top 样本月销量外推预测(预测值,假设随结果登记)")
    p.add_argument("--project", required=True)
    p.add_argument("--scope", choices=["top_sample", "amazon_channel", "full_market"], default="top_sample",
                   help="输入序列口径;market_metrics 只覆盖 Top 样本,故仅 top_sample 可用")
    p.add_argument("--method", choices=["cagr", "linear"], default="cagr")
    p.add_argument("--horizon-years", type=int, required=True)
    p.add_argument("--granularity", choices=["monthly", "annual"], default="monthly",
                   help="annual 只聚合 12 个月齐全的年份(需 ≥2 个整年)")
    p.add_argument("--target-scope", choices=["amazon_channel", "full_market"], help="跨口径时的报告口径,必须配 --coverage-ratio")
    p.add_argument("--coverage-ratio", type=float, help="样本占目标口径的比例 (0,1],须配 --coverage-source")
    p.add_argument("--coverage-source", help="覆盖率的来源说明(必填于 --coverage-ratio)")
    p.add_argument("--low-growth", type=float, help="低情景:cagr=年增速小数(0.05=5%%),linear=每年增量(台)")
    p.add_argument("--high-growth", type=float, help="高情景,口径同 --low-growth")
    p.set_defaults(func=cmd_build_market_forecast)

    p = sub.add_parser("build-cross-channel", help="forecast 与 tiktok-units 都存在时,生成 Amazon×TikTok 事实整合(人工决策栏留空)")
    p.add_argument("--project", required=True)
    p.add_argument("--geo", help="tiktok-units 含多个 geo/segment 时指定")
    p.add_argument("--segment")
    p.set_defaults(func=cmd_build_cross_channel)

    p = sub.add_parser("dossier", help="生成研究资料包")
    p.add_argument("--project", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_dossier)

    return parser


def _project_json_files(pdir: Path) -> list[Path]:
    return sorted([*pdir.glob("*.json"), *(pdir / "external").glob("*.json")]) if pdir.is_dir() else []


def _rollback_project_files(pdir: Path, backups: dict[Path, bytes]) -> None:
    """事务回滚时让项目工件回到命令执行前:恢复被改写/删除的 json,删除命令新建的 json。"""
    for path in _project_json_files(pdir):
        if path not in backups:
            path.unlink(missing_ok=True)
    for path, raw in backups.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(raw)
            temporary = Path(handle.name)
        temporary.replace(path)


READ_ONLY_COMMANDS = {"login", "projects", "status", "init", "export-table"}


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    backups: dict[Path, bytes] = {}
    pdir: Path | None = None
    try:
        if args.command == "login":
            args.func(args)
            return
        svc = _service(args)
        if getattr(args, "project", None) and args.command not in READ_ONLY_COMMANDS:
            pdir = Path(args.root) / args.project
            backups = {path: path.read_bytes() for path in _project_json_files(pdir)}
        # One DB write transaction serializes a complete CLI command across processes,
        # including reviewed file reads. Status API is experimental; do not share roots.
        output = io.StringIO()
        with redirect_stdout(output):
            with svc.store.transaction(on_rollback=lambda: pdir and _rollback_project_files(pdir, backups)):
                args.func(args)
        print(output.getvalue(), end="")
    except SystemExit:
        if "output" in locals():
            print(output.getvalue(), end="")
        raise
    except (KeyError, ValueError, OSError, sqlite3.Error, ConnectorError, RuntimeError) as exc:
        logger.warning("command %s failed: %s: %s", args.command, type(exc).__name__, str(exc)[:300])
        _emit({"error": str(exc).strip('"'), "command": args.command})
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - 最后防线:始终输出 JSON 错误(详情只进日志),不泄漏 traceback 给调用方
        logger.exception("command %s crashed", args.command)
        _emit({"error": f"内部错误({type(exc).__name__});详情见服务端日志", "command": args.command})
        sys.exit(1)
    finally:
        if hasattr(args, "_workflow_service"):
            args._workflow_service.store.close()


if __name__ == "__main__":
    main()
