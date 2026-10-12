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
import json
import os
import re
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
from research_workflow.competition import build_competition_metrics
from research_workflow.competition.models import CompetitionMetricsResult
from research_workflow.data import MonthlyBSRConfig, build_monthly_top
from research_workflow.data.io import read_tabular_rows
from research_workflow.data.models import BSRRecord, MonthlyBSRResult
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
    return WorkflowService(SQLiteStore(args.db))


def _project_dir(args: argparse.Namespace, project_id: str) -> Path:
    path = Path(args.root) / project_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def _require_file(path: Path, message: str) -> Any:
    if not path.exists():
        raise SystemExit(_emit({"error": message, "missing": str(path)}) or 1)
    return _load_json(path)


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


def _load_records(pdir: Path) -> list[BSRRecord]:
    by_month: dict[str, list[dict[str, Any]]] = _require_file(
        pdir / "records.json", "尚无数据;先运行 ingest"
    )
    records: list[BSRRecord] = []
    for month in sorted(by_month):
        records.extend(rebuild(BSRRecord, row) for row in by_month[month])
    return records


def _parse_kv(items: list[str], what: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        key, _, value = item.partition("=")
        if not key.strip() or not value.strip():
            raise SystemExit(_emit({"error": f"非法 {what} 格式(应为 key=value): {item}"}) or 1)
        parsed[key.strip()] = value.strip()
    return parsed


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
        }
        summary[result.month] = {
            "input_rows": result.stats.input_rows,
            "output_rows": result.stats.output_rows,
            "duplicate_parent_rows_removed": result.quality.duplicate_parent_rows_removed,
            "duplicate_title_rows_removed": result.quality.duplicate_title_rows_removed,
            "warnings": result.quality.warning_count,
        }
    all_sources.update(sources)

    _save_json(pdir / "records.json", by_month)
    _save_json(pdir / "quality.json", quality)
    _save_json(pdir / "sources.json", all_sources)
    svc.set_step(args.project, "collect_bsr", StepStatus.COMPLETED, output_ref="records.json")
    checkpoint = svc.request_human_checkpoint(
        args.project,
        "data_review",
        "请检查清洗后的月度数据(行数、去重、质量警告),确认后继续。",
        {"months": summary, "sources": list(all_sources.values()), "source_mode": source_mode},
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


async def _fetch_live(args: argparse.Namespace, pdir: Path) -> tuple[list[MonthlyBSRResult], dict[str, Any]]:
    _load_dotenv(ROOT / ".env")
    gateway = _build_gateway(ROOT / "config" / "mcp.json")
    connector = SellerSpriteConnector(gateway)
    await connector.validate()
    try:
        await connector.session_status()
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
    months = [m.strip() for m in args.months.split(",") if m.strip()]
    results: list[MonthlyBSRResult] = []
    sources: dict[str, Any] = {}
    raw_dir = pdir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for month in months:
        response = await connector.product_research(
            marketplace=args.marketplace,
            keyword=args.keyword,
            month=month,
            page_size=args.page_size,
            max_pages=args.max_pages,
        )
        rows = response.data.get("items", []) if isinstance(response.data, dict) else []
        raw_path = raw_dir / f"{month}.json"
        _save_json(raw_path, {"provider": "sellersprite", "operation": "product_research", "rows": rows})
        results.append(build_monthly_top(rows, month, MonthlyBSRConfig(top_n=args.top_n)))
        artifact_id = f"{month}:sellersprite"
        sources[artifact_id] = {
            "artifact_id": artifact_id,
            "name": f"sellersprite product_research {month}",
            "kind": "mcp",
            "source": "sellersprite",
            "sha256": _sha256_file(raw_path),
        }
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
        if not args.marketplace:
            args.marketplace = project.marketplace
        results, sources = asyncio.run(_fetch_live(args, pdir))
        _persist_ingest(svc, args, pdir, results, sources, source_mode="mcp:sellersprite")
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
        rows = read_tabular_rows(path)
        results.append(build_monthly_top(rows, month, MonthlyBSRConfig(top_n=args.top_n)))
        artifact_id = f"{month}:{path.name}"
        sources[artifact_id] = {
            "artifact_id": artifact_id,
            "name": path.name,
            "kind": path.suffix.lstrip(".") or "file",
            "source": str(path.resolve()),
            "sha256": _sha256_file(path),
        }
    _persist_ingest(svc, args, pdir, results, sources, source_mode="file")


def cmd_login(args: argparse.Namespace) -> None:
    _load_dotenv(ROOT / ".env")
    connector = SellerSpriteConnector(_build_gateway(ROOT / "config" / "mcp.json"))
    _emit({"hint": "请在弹出的 CDP Chrome 中登录卖家精灵;等待时间最长 " + str(args.wait) + " 秒"})
    result = asyncio.run(connector.validate() and connector.login(wait_seconds=args.wait))
    _emit({"login": "completed", "detail": str(result)[:2000]})


def cmd_classify(args: argparse.Namespace) -> None:
    svc = _service(args)
    if svc.store.get_project(args.project) is None:
        raise KeyError(f"Unknown project: {args.project}")
    pdir = _project_dir(args, args.project)
    records = _load_records(pdir)

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
    # 离线模式:画像直接来自 BSR 行,跳过 MCP 补全(实连见 references/live-mcp.md)。
    svc.set_step(args.project, "enrich_asins", StepStatus.SKIPPED)

    labels = [
        ClassificationLabel(key=key, name=name)
        for key, name in _parse_kv(args.label, "--label").items()
    ]
    schema = ClassificationSchema(project_id=args.project, dimension=args.dimension, labels=labels)
    rules = {
        key: [word.strip() for word in value.split(",") if word.strip()]
        for key, value in _parse_kv(args.rule, "--rule").items()
    } if args.rule else {}
    service = ClassificationService(RuleClassificationProvider(rules))
    batch = asyncio.run(service.classify(schema, list(profiles.values())))

    _save_json(pdir / "classification.json", {"schema": schema.canonical_payload(), "batch": batch.to_dict()})
    svc.set_step(args.project, "classify", StepStatus.COMPLETED, output_ref="classification.json")
    checkpoint = service.request_review_checkpoint(svc, schema, batch)

    counts: dict[str, int] = {}
    for row in batch.rows.values():
        counts[row.label] = counts.get(row.label, 0) + 1
    _emit(
        {
            "project_id": args.project,
            "classification_version": batch.version,
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
    elif "collect_bsr" not in completed:
        next_action = "ingest"
    elif "classify" not in completed:
        next_action = "classify"
    elif "market_metrics" not in completed:
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
    if checkpoint.status.value != "pending":
        raise ValueError(f"Checkpoint 已裁决: {checkpoint.status.value}")

    pdir = _project_dir(args, args.project)
    response: dict[str, Any] = {"approved": args.approve}

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
        _save_json(pdir / "classification.json", {"schema": envelope["schema"], "batch": batch.to_dict()})
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

    svc.resolve_human_checkpoint(args.checkpoint, approved=args.approve, response=response)

    next_action = {
        "data_review": "classify",
        "classification_review": "analyze",
        "route_review": "dossier",
        "research_route_review": "dossier",
    }.get(checkpoint.step_key, "status")
    if not args.approve:
        next_action = f"人工驳回({checkpoint.step_key});修正后重新执行对应阶段或再次 resolve"
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
        rows = read_tabular_rows(args.new_products_file)
        snapshots = [
            NewProductSnapshot(
                asin=str(_col(row, "ASIN", "asin")).strip().upper(),
                month=str(_col(row, "月份", "month")),
                monthly_sales=int(float(str(_col(row, "月销量", "monthly_sales", "销量", "sales")).replace(",", ""))),
                launch_date=_col(row, "上架时间", "launch_date") or None,
                seller_region=str(_col(row, "卖家所属地", "seller_region") or "UNKNOWN"),
                account_type=str(_col(row, "账号类型", "account_type") or "UNKNOWN"),
                segment=str(_col(row, "细分", "segment") or "unknown"),
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

    _save_json(pdir / "market_metrics.json", market.to_dict())
    _save_json(pdir / "market_analysis.json", analysis.to_dict())
    _save_json(pdir / "competition.json", competition.to_dict())
    if new_products is not None:
        _save_json(pdir / "new_products.json", new_products.to_dict())
    _save_json(pdir / "route_plan.json", route_plan.to_dict())

    svc.set_step(args.project, "market_metrics", StepStatus.COMPLETED, output_ref="market_metrics.json")
    svc.set_step(args.project, "market_analysis", StepStatus.COMPLETED, output_ref="market_analysis.json")
    svc.set_step(args.project, "competition_metrics", StepStatus.COMPLETED, output_ref="competition.json")
    svc.set_step(
        args.project,
        "new_product_analysis",
        StepStatus.COMPLETED if new_products is not None else StepStatus.SKIPPED,
    )
    svc.set_step(args.project, "research_route", StepStatus.COMPLETED, output_ref="route_plan.json")
    checkpoint = ResearchRouter().request_human_checkpoint(svc, args.project, route_plan)

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
    sources = [rebuild(SourceArtifact, item) for item in _load_json(pdir / "sources.json").values()]

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
    missing_evidence: list[str] = []
    if new_products is None:
        missing_evidence.append("未提供新品跟踪表(new_products),T09 新品结论缺失")

    output = Path(args.output)
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
            source_artifacts=sources,
            data_quality=data_quality,
            missing_evidence=missing_evidence,
            metadata={"classification_version": _load_json(pdir / "classification.json")["batch"]["version"]},
        ),
        output,
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
    p.add_argument("--months", help="live 模式月份列表,如 2025-01,2026-01")
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--max-pages", type=int, default=20)
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("login", help="触发 SellerSprite MCP 登录流程(CDP Chrome)")
    p.add_argument("--wait", type=int, default=180)
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("classify", help="运行产品分类并请求人工复核")
    p.add_argument("--project", required=True)
    p.add_argument("--label", action="append", required=True, help="标签 key=中文名,可多次")
    p.add_argument("--dimension", default="segment")
    p.add_argument("--rule", action="append", default=[], help="规则 key=关键词1,关键词2,可多次")
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


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (KeyError, ValueError, FileNotFoundError) as exc:
        _emit({"error": str(exc).strip('"'), "command": args.command})
        sys.exit(1)


if __name__ == "__main__":
    main()
