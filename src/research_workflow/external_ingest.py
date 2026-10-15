"""外部证据登记与预测接线(把 market_external / competition_external 两个纯库接入项目目录)。

登记的是「证据」,不是人工检查点:external/ 下的文件不参与 data_review /
classification_review 的哈希,但 dossier 导出时必须带入 evidence/ 与 manifest,
并在导出前校验 external/index.json 登记的哈希。本模块不联网,不产出任何商业结论。

目录布局(均在项目目录下):
  external/<kind>.json     {"kind", "entries":[{seq,file_name,sha256,imported_at,record_count,summary,meta,data}]}
  external/index.json      {"entries":[每次导入一行], "files":{"<name>.json": sha256}}
  external/forecast.json   build-market-forecast 的输出(含可重放的参数)
  external/cross_channel.json  build-cross-channel 的事实整合
  raw/<sha256><后缀>        导入文件的原始快照(与 ingest 同一套快照规则)
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from research_workflow.competition_external import (
    EvidenceRef,
    OffsiteLedger,
    PatentRecord,
    BrandAsin,
    check_big_brand,
    load_ad_records,
    load_brand_asins,
    load_cross_category_asins,
    load_offsite_searches,
    load_patent_records,
    load_patent_search_logs,
    sha256_of_file,
    summarize_cross_category,
)
from research_workflow.market_external import (
    CoverageAssumption,
    EvidenceLedger,
    ExternalEvidence,
    ScopeLevel,
    SeriesPoint,
    UnitsRecord,
    analyze_tiktok,
    build_cross_channel_summary,
    check_trends_consistency,
    extrapolate,
    import_report_excerpts,
    load_trends_csv,
    load_units_file,
    trends_to_evidence,
)
from research_workflow.market_external.trends import GoogleTrendsExport, TrendSeries
from research_workflow.voc_external import (
    load_keyword_rows,
    load_scenario_rows,
    load_voc_rows,
    summarize_keywords,
    summarize_scenarios,
    summarize_voc,
)

EXTERNAL_DIR = "external"
INDEX_NAME = "index.json"
FORECAST_NAME = "forecast.json"
CROSS_CHANNEL_NAME = "cross_channel.json"
KINDS = (
    "trends", "report-excerpts", "tiktok-units", "patents", "patent-search-log",
    "ads", "big-brand", "offsite", "cross-category", "keywords", "scenarios", "voc",
)
_NONE: tuple[str, ...] = ()
# kind -> (必填 meta, 可选 meta)
META_SPEC: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "trends": (("geo", "timeframe", "source_name", "source_url", "as_of"), ("keywords",)),
    "report-excerpts": (_NONE, _NONE),
    "tiktok-units": (("source_name", "source_url", "as_of"), ("low_growth", "high_growth")),
    "patents": (_NONE, _NONE),
    "patent-search-log": (_NONE, _NONE),
    "ads": (_NONE, _NONE),
    "big-brand": (("brand", "product_type", "source_name", "retrieved_at"), ("aba_rank", "threshold", "url", "as_of")),
    "offsite": (_NONE, _NONE),
    "cross-category": (("source_name", "retrieved_at"), ("url", "as_of", "brand")),
    # T6:取数范围(scope)必填——样本不是总体,缺范围的数字没法被人复核
    "keywords": (("source_name", "retrieved_at", "scope"), ("url",)),
    "scenarios": (("source_name", "retrieved_at", "scope"), ("url",)),
    "voc": (("source_name", "retrieved_at", "scope"), ("url",)),
}


# ------------------------------------------------------------------ 文件工具


def _write_json(path: Path, payload: Any) -> None:
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


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _file_sha(path: Path) -> str:
    return sha256_of_file(path)


def _json_safe(payload: Any) -> Any:
    """Enum / 元组等规整为纯 JSON 结构,保证 external/*.json 可读可哈希。"""
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))


def _ext_dir(pdir: Path) -> Path:
    return pdir / EXTERNAL_DIR


def load_index(pdir: Path) -> dict[str, Any]:
    path = _ext_dir(pdir) / INDEX_NAME
    return _read_json(path) if path.is_file() else {"entries": [], "files": {}}


def _load_kind(pdir: Path, kind: str) -> list[dict[str, Any]]:
    path = _ext_dir(pdir) / f"{kind}.json"
    return list(_read_json(path)["entries"]) if path.is_file() else []


def _register_file(pdir: Path, name: str, payload: Any, index_entries: list[dict[str, Any]] | None = None) -> None:
    """写 external/<name> 并把其哈希登记进 index.json(index 总是最后写)。"""
    path = _ext_dir(pdir) / name
    _write_json(path, payload)
    index = load_index(pdir)
    index["files"][name] = _file_sha(path)
    if index_entries:
        index["entries"].extend(index_entries)
    _write_json(_ext_dir(pdir) / INDEX_NAME, index)


# ------------------------------------------------------------------ 导入


def validate_meta(kind: str, meta: dict[str, str]) -> dict[str, str]:
    required, optional = META_SPEC[kind]
    missing = [key for key in required if not meta.get(key)]
    if missing:
        raise ValueError(f"{kind} 导入缺少来源/口径信息 --meta: {missing}")
    unknown = sorted(set(meta) - set(required) - set(optional))
    if unknown:
        raise ValueError(f"{kind} 不接受 --meta 键 {unknown};允许: {sorted({*required, *optional})}")
    return dict(meta)


def _iso(value: str, name: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"--meta {name} 必须是 ISO 日期 YYYY-MM-DD: {value!r}") from exc
    return value


def _float(value: str, name: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"--meta {name} 必须是数字: {value!r}") from exc


def _evidence_ref(path: Path, meta: dict[str, str]) -> EvidenceRef:
    """文件级证据引用:来源名/取数日期来自 --meta,文件路径与 SHA256 取自导入文件本身。"""
    return EvidenceRef(
        source_name=meta["source_name"],
        retrieved_at=meta["retrieved_at"],
        url=meta.get("url"),
        file_path=str(path.resolve()),
        sha256=sha256_of_file(path),
    )


def _parse_trends(path: Path, meta: dict[str, str], **_: Any) -> tuple[Any, int, str]:
    keywords = [k.strip() for k in meta["keywords"].split(",") if k.strip()] if meta.get("keywords") else None
    export = load_trends_csv(
        path, geo=meta["geo"], timeframe=meta["timeframe"], source_name=meta["source_name"],
        source_url=meta["source_url"], as_of=meta["as_of"], keywords=keywords,
    )
    trends_to_evidence(export)  # 触发 as_of 等字段校验
    count = sum(len(series.monthly) for series in export.series)
    return asdict(export), count, f"{len(export.series)} 个关键词 / {count} 个月度点({export.geo},{export.timeframe})"


def _parse_report_excerpts(path: Path, meta: dict[str, str], *, prior: list[dict[str, Any]], seq: int, **_: Any):
    taken = {item["claim_id"] for entry in prior for item in entry["data"]}
    items: list[ExternalEvidence] = []
    for item in import_report_excerpts(path):
        match = re.fullmatch(r"report:(\d+)", item.claim_id)
        if item.claim_id in taken and match:  # 库默认编号与更早导入冲突时按导入序号加前缀
            item = dataclasses.replace(item, claim_id=f"report:s{seq}:{match.group(1)}")
        items.append(item)
    ledger = EvidenceLedger()
    for item in items:
        if item.claim_id in taken:
            raise ValueError(f"claim_id 与更早导入重复: {item.claim_id}")
        ledger.add(item)
    ledger.require_sources()  # present 必须有来源名/链接/日期
    usable = sum(1 for item in items if item.status == "present")
    return [asdict(item) for item in items], len(items), f"可用 {usable} 条 / 来源不可访问 {len(items) - usable} 条"


def _combined_tiktok(pdir: Path, extra: list[dict[str, Any]] | None = None) -> tuple[list[UnitsRecord], dict[str, dict[str, str]], float | None, float | None]:
    """合并所有 tiktok-units 导入:同一 (geo,segment,channel,period) 后导入的覆盖先导入的(历史条目仍保留)。"""
    merged: dict[tuple[str, str, str, str], UnitsRecord] = {}
    sources: dict[str, dict[str, str]] = {}
    low = high = None
    for entry in [*_load_kind(pdir, "tiktok-units"), *(extra or [])]:
        meta = entry["meta"]
        for row in entry["data"]["records"]:
            record = UnitsRecord(**row)
            merged[(record.geo, record.segment, record.channel, record.period)] = record
            sources[record.evidence_id] = {key: meta[key] for key in ("source_name", "source_url", "as_of")}
        low = float(meta["low_growth"]) if meta.get("low_growth") else low
        high = float(meta["high_growth"]) if meta.get("high_growth") else high
    return list(merged.values()), sources, low, high


def _tiktok_pairs(records: list[UnitsRecord]) -> list[tuple[str, str]]:
    return sorted({(r.geo, r.segment) for r in records})


def _parse_tiktok(path: Path, meta: dict[str, str], *, pdir: Path, seq: int, **_: Any):
    _iso(meta["as_of"], "as_of")
    for key in ("low_growth", "high_growth"):
        if meta.get(key):
            _float(meta[key], key)
    records = load_units_file(path)
    if not records:
        raise ValueError("文件没有可导入的记录")
    entry = {"data": {"records": [asdict(r) for r in records]}, "meta": meta}
    # 试算:低/高增速与 CAGR 不自洽等库级错误在导入时就暴露,不留到导出
    combined, _, low, high = _combined_tiktok(pdir, [entry])
    for geo, segment in _tiktok_pairs(combined):
        analyze_tiktok(combined, geo=geo, segment=segment, low_growth=low, high_growth=high)
    pairs = "、".join(f"{g}/{s}" for g, s in _tiktok_pairs(records))
    return entry["data"], len(records), f"{len(records)} 条台数记录(台,不含 GMV);{pairs}"


def _prior_patent_records(pdir: Path) -> list[PatentRecord]:
    """检索日志要挂接专利记录:重放更早导入的专利文件快照。"""
    index = load_index(pdir)
    records: list[PatentRecord] = []
    for item in index["entries"]:
        if item["kind"] == "patents":
            records.extend(load_patent_records(pdir / item["snapshot"]))
    return records


def _parse_patents(path: Path, meta: dict[str, str], **_: Any):
    records = load_patent_records(path)
    return [r.to_dict() for r in records], len(records), f"{len(records)} 条专利记录(仅检索记录,不构成法律意见)"


def _parse_patent_logs(path: Path, meta: dict[str, str], *, pdir: Path, **_: Any):
    logs = load_patent_search_logs(path, _prior_patent_records(pdir))
    counts = {s: sum(1 for log in logs if log.status == s) for s in ("searched_found", "searched_none", "not_searched")}
    summary = f"已检索-发现 {counts['searched_found']} / 已检索-范围内未发现 {counts['searched_none']} / 未检索 {counts['not_searched']}"
    return [log.to_dict() for log in logs], len(logs), summary


def _parse_ads(path: Path, meta: dict[str, str], **_: Any):
    records = load_ad_records(path)
    return [r.to_dict() for r in records], len(records), f"{len(records)} 个人工指定 ASIN"


def _parse_big_brand(path: Path, meta: dict[str, str], **_: Any):
    asins: list[BrandAsin] = load_brand_asins(path)
    aba = int(_float(meta["aba_rank"], "aba_rank")) if meta.get("aba_rank") else None
    kwargs: dict[str, Any] = {}
    if meta.get("threshold"):
        kwargs["threshold"] = int(_float(meta["threshold"], "threshold"))
    if meta.get("as_of"):
        kwargs["as_of"] = date.fromisoformat(_iso(meta["as_of"], "as_of"))
    check = check_big_brand(meta["brand"], meta["product_type"], aba, asins, _evidence_ref(path, meta), **kwargs)
    return check.to_dict(), len(asins), f"{meta['brand']} / {meta['product_type']}:{check.status}"


def _parse_offsite(path: Path, meta: dict[str, str], **_: Any):
    searches = load_offsite_searches(path)
    brands: dict[str, list] = {}
    for search in searches:
        brands.setdefault(search.brand.strip().lower(), []).append(search)
    ledgers = [OffsiteLedger(items[0].brand, tuple(items)).to_dict() for items in brands.values()]
    return ledgers, len(searches), f"{len(brands)} 个品牌 / {len(searches)} 次检索"


def _parse_cross_category(path: Path, meta: dict[str, str], **_: Any):
    asins = load_cross_category_asins(path)
    as_of = date.fromisoformat(_iso(meta["as_of"], "as_of")) if meta.get("as_of") else None
    data = summarize_cross_category(asins, _evidence_ref(path, meta), as_of=as_of)
    data["brand"] = meta.get("brand")
    return data, len(asins), f"{data['category_count']} 个类目 / {len(asins)} 个 ASIN"


def _demand_entry(path: Path, meta: dict[str, str], rows: list[Any], summary: dict[str, Any]) -> dict[str, Any]:
    _iso(meta["retrieved_at"], "retrieved_at")
    return {"records": [r.to_dict() for r in rows], "summary": summary,
            "evidence": {**_evidence_ref(path, meta).to_dict(), "scope": meta["scope"]}}


def _parse_keywords(path: Path, meta: dict[str, str], **_: Any):
    rows = load_keyword_rows(path)
    summary = summarize_keywords(rows)
    return (_demand_entry(path, meta, rows, summary), len(rows),
            f"{summary['keyword_count']} 个关键词(缺搜索量 {summary['missing_volume_keywords']});范围: {meta['scope']}")


def _parse_scenarios(path: Path, meta: dict[str, str], **_: Any):
    rows = load_scenario_rows(path)
    summary = summarize_scenarios(rows)
    return (_demand_entry(path, meta, rows, summary), len(rows),
            f"{summary['scenario_count']} 个场景 / 提及合计 {summary['mentions_total']:g};范围: {meta['scope']}")


def _parse_voc(path: Path, meta: dict[str, str], **_: Any):
    rows = load_voc_rows(path)
    summary = summarize_voc(rows)
    return (_demand_entry(path, meta, rows, summary), len(rows),
            f"{summary['excerpt_count']} 条评论摘录 / {summary['theme_count']} 个主题;范围: {meta['scope']}")


_PARSERS = {
    "trends": _parse_trends, "report-excerpts": _parse_report_excerpts, "tiktok-units": _parse_tiktok,
    "patents": _parse_patents, "patent-search-log": _parse_patent_logs, "ads": _parse_ads,
    "big-brand": _parse_big_brand, "offsite": _parse_offsite, "cross-category": _parse_cross_category,
    "keywords": _parse_keywords, "scenarios": _parse_scenarios, "voc": _parse_voc,
}


def prepare_import(pdir: Path, kind: str, file: Path, meta: dict[str, str], imported_at: str) -> dict[str, Any]:
    """只解析校验、不写任何文件;库抛出的 ValueError 原样上抛,保证失败不改状态。"""
    if kind not in _PARSERS:
        raise ValueError(f"未知证据类别: {kind};可选: {list(KINDS)}")
    if not file.is_file():
        raise ValueError(f"文件不存在: {file}")
    meta = validate_meta(kind, meta)
    prior = _load_kind(pdir, kind)
    seq = len(prior) + 1
    try:
        data, count, summary = _PARSERS[kind](file, meta, pdir=pdir, prior=prior, seq=seq)
    except (TypeError, AttributeError) as exc:
        raise ValueError(f"文件结构无法解析为 {kind}: {exc}") from exc
    if count < 1:
        raise ValueError("文件没有可导入的记录")
    digest = sha256_of_file(file)
    same = [item["seq"] for item in prior if item["sha256"] == digest]
    if same:
        summary += f";与第 {same[0]} 次导入的文件内容相同(重复计入,请确认)"
    return {
        "seq": seq, "file_name": file.name, "sha256": digest, "imported_at": imported_at,
        "record_count": count, "summary": summary, "meta": meta, "data": _json_safe(data),
    }


def commit_import(pdir: Path, kind: str, entry: dict[str, Any], snapshot: Path) -> dict[str, Any]:
    """prepare_import 通过且原文件已快照后落盘:追加条目 + 登记索引哈希。"""
    entries = _load_kind(pdir, kind)
    entries.append(entry)
    index_row = {
        "kind": kind, "seq": entry["seq"], "file_name": entry["file_name"], "sha256": entry["sha256"],
        "snapshot": snapshot.relative_to(pdir).as_posix(), "imported_at": entry["imported_at"],
        "record_count": entry["record_count"], "summary": entry["summary"],
    }
    _register_file(pdir, f"{kind}.json", {"kind": kind, "entries": entries}, [index_row])
    return index_row


# ------------------------------------------------------------------ 预测


def _market_series(pdir: Path, granularity: str) -> list[SeriesPoint]:
    path = pdir / "market_metrics.json"
    if not path.is_file():
        raise ValueError(f"缺少 market_metrics.json;先完成 analyze: {path}")
    monthly = {row["month"]: float(row["total_sales"]) for row in _read_json(path)["market"]}
    if granularity == "monthly":
        return [SeriesPoint(month, value) for month, value in sorted(monthly.items())]
    by_year: dict[str, list[float]] = {}
    for month, value in monthly.items():
        by_year.setdefault(month[:4], []).append(value)
    complete = {year: sum(values) for year, values in sorted(by_year.items()) if len(values) == 12}
    if len(complete) < 2:
        raise ValueError(f"按年预测至少需要两个 12 个月齐全的年份;当前齐全年份: {sorted(complete)}")
    return [SeriesPoint(year, value) for year, value in complete.items()]


def _run_extrapolate(params: dict[str, Any]):
    """由登记的参数重放预测(forecast 与 cross-channel 共用,保证确定性)。"""
    coverage = None
    if params.get("coverage"):
        cov = params["coverage"]
        coverage = CoverageAssumption(cov["ratio"], cov["source"], ScopeLevel(cov["from_scope"]), ScopeLevel(cov["to_scope"]))
    return extrapolate(
        [SeriesPoint(p, v) for p, v in params["series"]], ScopeLevel(params["scope"]),
        method=params["method"], horizon_years=params["horizon_years"],
        scenarios=params["scenarios"] or None,
        target_scope=ScopeLevel(params["target_scope"]), coverage=coverage,
        evidence_ids=params["evidence_ids"], unit="units",
    )


def build_forecast(
    pdir: Path, *, scope: str, method: str, horizon_years: int, granularity: str,
    coverage_ratio: float | None, coverage_source: str | None, target_scope: str | None,
    low_growth: float | None, high_growth: float | None, today: date, created_at: str,
) -> dict[str, Any]:
    if scope != ScopeLevel.TOP_SAMPLE.value:
        raise ValueError("market_metrics.json 的月度销量只覆盖 Top 样本,--scope 只能是 top_sample;"
                         "要报告更大口径请用 --target-scope 并给出带来源的 --coverage-ratio")
    if coverage_ratio is not None and target_scope is None:
        raise ValueError("--coverage-ratio 只在 --target-scope 跨口径时使用")
    target = target_scope or scope
    series = _market_series(pdir, granularity)
    digest = _file_sha(pdir / "market_metrics.json")
    evidence_id = f"market_metrics@{digest[:12]}"
    coverage = None
    if coverage_ratio is not None:
        coverage = {"ratio": coverage_ratio, "source": coverage_source or "", "from_scope": scope, "to_scope": target}
    params = {
        "series": [[p.period, p.value] for p in series], "scope": scope, "method": method,
        "horizon_years": horizon_years, "granularity": granularity,
        "scenarios": {k: v for k, v in (("low", low_growth), ("high", high_growth)) if v is not None},
        "target_scope": target, "coverage": coverage, "evidence_ids": [evidence_id],
    }
    result = _run_extrapolate(params)  # 库在此拒绝缺覆盖率的跨口径外推
    doc = {
        "params": params, "result": _json_safe(result.to_dict()),
        "evidence": {"id": evidence_id, "source_name": "market_metrics.json(本系统 Top 样本月销量)",
                     "source_url": "file://market_metrics.json", "as_of": today.isoformat()},
        "source_market_metrics_sha256": digest, "created_at": created_at,
    }
    _register_file(pdir, FORECAST_NAME, doc)
    return doc


# ------------------------------------------------------------------ 跨渠道事实整合


def _pick_pair(records: list[UnitsRecord], geo: str | None, segment: str | None) -> tuple[str, str]:
    pairs = _tiktok_pairs(records)
    if geo and segment:
        if (geo, segment) not in pairs:
            raise ValueError(f"tiktok-units 中没有 {geo}/{segment};已有: {pairs}")
        return geo, segment
    if len(pairs) == 1:
        return pairs[0]
    raise ValueError(f"tiktok-units 含多个 geo/segment {pairs};请用 --geo 与 --segment 指定")


def _cross_channel_ledger(pdir: Path, forecast: dict[str, Any], records: list[UnitsRecord],
                          sources: dict[str, dict[str, str]]) -> EvidenceLedger:
    ledger = EvidenceLedger()
    for entry in _load_kind(pdir, "report-excerpts"):
        for item in entry["data"]:
            ledger.add(ExternalEvidence(**item))
    ev = forecast["evidence"]
    ledger.add(ExternalEvidence(ev["id"], "top_sample_monthly_sales", forecast["params"]["series"][-1][1], "units",
                                forecast["params"]["series"][-1][0], ev["source_name"], ev["source_url"],
                                ev["as_of"], "file"))
    for record in records:
        if record.evidence_id in ledger:
            continue  # 用户已用同名研报摘录登记过该证据
        src = sources[record.evidence_id]
        ledger.add(ExternalEvidence(record.evidence_id, "tiktok_units", record.units, "units", record.period,
                                    src["source_name"], src["source_url"], src["as_of"], "file"))
    return ledger


def build_cross_channel(pdir: Path, *, geo: str | None, segment: str | None, created_at: str) -> dict[str, Any]:
    forecast_path = _ext_dir(pdir) / FORECAST_NAME
    if not forecast_path.is_file():
        raise ValueError("缺少 external/forecast.json;先运行 build-market-forecast")
    records, sources, low, high = _combined_tiktok(pdir)
    if not records:
        raise ValueError("尚未导入 tiktok-units;先运行 import-evidence --kind tiktok-units")
    geo, segment = _pick_pair(records, geo, segment)
    forecast = _read_json(forecast_path)
    analysis = analyze_tiktok(records, geo=geo, segment=segment, low_growth=low, high_growth=high)
    ledger = _cross_channel_ledger(pdir, forecast, records, sources)
    summary = build_cross_channel_summary(_run_extrapolate(forecast["params"]), analysis, ledger)
    doc = {
        "summary": _json_safe(summary.to_dict()), "geo": geo, "segment": segment, "created_at": created_at,
        "inputs": {FORECAST_NAME: _file_sha(forecast_path), "tiktok-units.json": _file_sha(_ext_dir(pdir) / "tiktok-units.json")},
    }
    _register_file(pdir, CROSS_CHANNEL_NAME, doc)
    return doc


# ------------------------------------------------------------------ dossier 接入


def empty_external() -> dict[str, Any]:
    """尚无任何外部证据时的骨架(dossier 据此逐项列出「尚未提供」)。"""
    return {"index": [], **{kind: [] for kind in KINDS}, "forecast": None, "cross_channel": None,
            "tiktok_analysis": [], "tiktok_sources": {}, "trends_consistency": []}


def _verify_registered(pdir: Path) -> dict[str, Any]:
    index = load_index(pdir)
    directory = _ext_dir(pdir)
    registered = index["files"]
    for path in sorted(directory.glob("*.json")):
        if path.name != INDEX_NAME and path.name not in registered:
            raise ValueError(f"external/{path.name} 未登记在 index.json;请重新导入或移除该文件")
    for name, digest in registered.items():
        path = directory / name
        if Path(name).name != name or not path.is_file() or _file_sha(path) != digest:
            raise ValueError(f"外部证据文件被修改或缺失: external/{name};请重新导入")
    for item in index["entries"]:
        snapshot = pdir / item["snapshot"]
        if not snapshot.is_file() or _file_sha(snapshot) != item["sha256"]:
            raise ValueError(f"外部证据原始快照缺失或哈希不匹配: {item['snapshot']};请重新导入")
    return index


def load_external(pdir: Path) -> dict[str, Any] | None:
    """校验并装载 external/ 供 dossier 使用;目录不存在返回 None。篡改/过期一律报错。"""
    directory = _ext_dir(pdir)
    if not directory.is_dir():
        return None
    if not (directory / INDEX_NAME).is_file():
        raise ValueError("external/ 存在但缺少 index.json;外部证据无法校验")
    index = _verify_registered(pdir)
    ext = empty_external()
    ext["index"] = index["entries"]
    for kind in KINDS:
        ext[kind] = _load_kind(pdir, kind)
    metrics_sha = _file_sha(pdir / "market_metrics.json") if (pdir / "market_metrics.json").is_file() else None
    if (directory / FORECAST_NAME).is_file():
        ext["forecast"] = _read_json(directory / FORECAST_NAME)
        if ext["forecast"]["source_market_metrics_sha256"] != metrics_sha:
            raise ValueError("external/forecast.json 基于旧的 market_metrics.json,已过期;请重新运行 build-market-forecast")
    if (directory / CROSS_CHANNEL_NAME).is_file():
        ext["cross_channel"] = _read_json(directory / CROSS_CHANNEL_NAME)
        current = {FORECAST_NAME: index["files"].get(FORECAST_NAME), "tiktok-units.json": index["files"].get("tiktok-units.json")}
        if ext["cross_channel"]["inputs"] != current:
            raise ValueError("external/cross_channel.json 基于旧的预测或台数数据,已过期;请重新运行 build-cross-channel")
    records, sources, low, high = _combined_tiktok(pdir)
    ext["tiktok_sources"] = sources
    ext["tiktok_analysis"] = [
        _json_safe(analyze_tiktok(records, geo=geo, segment=segment, low_growth=low, high_growth=high).to_dict())
        for geo, segment in _tiktok_pairs(records)
    ]
    exports = [_trends_export(entry["data"]) for entry in ext["trends"]]
    ext["trends_consistency"] = check_trends_consistency(exports)
    return ext


def _trends_export(data: dict[str, Any]) -> GoogleTrendsExport:
    series = tuple(
        TrendSeries(s["keyword"], tuple(tuple(p) for p in s["monthly"]), tuple(tuple(p) for p in s["yoy"]))
        for s in data["series"]
    )
    return GoogleTrendsExport(data["geo"], data["timeframe"], tuple(data["keywords"]), series,
                              data["source_name"], data["source_url"], data["as_of"])


def evidence_files(pdir: Path) -> dict[str, Path]:
    """需要复制进 dossier evidence/ 的外部证据文件(扁平文件名)。"""
    directory = _ext_dir(pdir)
    if not directory.is_dir():
        return {}
    files = {f"external_{path.name}": path for path in sorted(directory.glob("*.json"))}
    for item in load_index(pdir)["entries"]:
        snapshot = pdir / item["snapshot"]
        files[snapshot.name] = snapshot
    return files
