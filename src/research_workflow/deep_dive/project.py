"""deep_dive 与项目目录的接线:落盘 deep_dive.json(绑定输入哈希),dossier 导出前校验未过期。

deep_dive.json 是派生工件,不是人工检查点:它汇总已算指标与已登记证据,输入一变就必须重跑。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_workflow.competition_external.ledger import sha256_of_file
from research_workflow.external_ingest import EXTERNAL_DIR, INDEX_NAME, load_external
from research_workflow.router.models import ResearchRoutePlan, RouteSuggestion
from research_workflow.router.service import confirmed_modules

from .engine import Inputs, run_deep_dive

DEEP_DIVE_FILE = "deep_dive.json"
# 输入工件:任何一个变化(重新 analyze / 新导入外部证据)都使既有深挖过期
_INPUT_FILES = ("market_metrics.json", "competition.json", "new_products.json", "analysis_context.json")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def input_hashes(pdir: Path) -> dict[str, str | None]:
    hashes = {name: sha256_of_file(pdir / name) if (pdir / name).is_file() else None for name in _INPUT_FILES}
    index = pdir / EXTERNAL_DIR / INDEX_NAME
    hashes[f"{EXTERNAL_DIR}/{INDEX_NAME}"] = sha256_of_file(index) if index.is_file() else None
    return hashes


def resolve_modules(pdir: Path, explicit: list[str]) -> tuple[list[str], list[str]]:
    """返回 (已确认路线 id, 模块队列):人工确认的路线展开的模块在前,显式 --modules 追加,去重保序。"""
    plan_path = pdir / "route_plan.json"
    route_ids: list[str] = []
    modules: list[str] = []
    if plan_path.is_file():
        data = _read(plan_path)
        if data.get("human_confirmed"):
            plan = ResearchRoutePlan(
                suggestions=[RouteSuggestion(**{**s, "evidence_ids": tuple(s["evidence_ids"]), "next_modules": tuple(s["next_modules"])})
                             for s in data["suggestions"]],
                confirmed_route_ids=list(data["confirmed_route_ids"]), human_confirmed=True,
            )
            route_ids = list(plan.confirmed_route_ids)
            modules = confirmed_modules(plan)
    for module in explicit:
        if module not in modules:
            modules.append(module)
    return route_ids, modules


def build(pdir: Path, explicit_modules: list[str], created_at: str) -> dict[str, Any]:
    if not (pdir / "market_metrics.json").is_file():
        raise ValueError("缺少 market_metrics.json;先完成 analyze")
    route_ids, modules = resolve_modules(pdir, explicit_modules)
    if not modules:
        raise ValueError("没有可执行的深挖模块:研究路线尚未人工确认,且未用 --modules 指定;"
                         "请先 resolve route_review(--select 选路线),或显式给出 --modules")
    external = load_external(pdir)  # 外部证据篡改/过期在这里就报错
    inputs = Inputs(
        market=_read(pdir / "market_metrics.json"),
        competition=_read(pdir / "competition.json") if (pdir / "competition.json").is_file() else None,
        new_products=_read(pdir / "new_products.json") if (pdir / "new_products.json").is_file() else None,
        external=external, context=_read(pdir / "analysis_context.json") if (pdir / "analysis_context.json").is_file() else {},
    )
    report = run_deep_dive(inputs, confirmed_route_ids=route_ids, modules=modules)
    doc = {"report": report.to_dict(), "inputs": input_hashes(pdir), "created_at": created_at}
    path = pdir / DEEP_DIVE_FILE
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return doc


def load_for_dossier(pdir: Path) -> dict[str, Any] | None:
    """dossier 用:文件不存在返回 None;输入已变化则报错,不静默带入过期深挖。"""
    path = pdir / DEEP_DIVE_FILE
    if not path.is_file():
        return None
    doc = _read(path)
    if doc["inputs"] != input_hashes(pdir):
        raise ValueError("deep_dive.json 基于旧的指标或外部证据,已过期;请重新运行 deep-dive")
    return doc
