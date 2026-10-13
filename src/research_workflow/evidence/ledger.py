from __future__ import annotations

import json
import re
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

from research_workflow.analysis.market import FORBIDDEN_DECISION_PATTERNS
from research_workflow.data.models import BSRRecord

from .models import EvidenceRecord, ModuleFinding, SlotStatus
from .slots import SLOTS, SlotSpec, required_slots

_UNIT_ALIASES = {
    "units": "units", "unit": "units", "台": "units", "台数": "units", "件": "units", "销量": "units",
    "usd": "usd", "$": "usd", "美元": "usd",
    "pct": "pct", "%": "pct", "percent": "pct", "百分比": "pct",
}
_GMV_WORDS = ("gmv", "销售额", "金额", "revenue")
CONFIDENCE = {"high", "medium", "low"}


def normalize_unit(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    text = raw.strip().lower()
    if any(word in text for word in _GMV_WORDS):
        return "usd"  # GMV/金额一律视为金额，不能冒充台数
    return _UNIT_ALIASES.get(text, text)


class EvidenceLedger:
    """文件型外部证据账本。只追加；每条证据必须有可追溯来源（URL 或带 SHA256 的文件）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.evidence: list[EvidenceRecord] = []
        self.findings: list[ModuleFinding] = []
        if path.exists():
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.evidence = [EvidenceRecord(**row) for row in doc.get("evidence", [])]
            self.findings = [
                ModuleFinding(**{**row, "evidence_ids": tuple(row["evidence_ids"])})
                for row in doc.get("findings", [])
            ]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"evidence": [e.to_dict() for e in self.evidence],
               "findings": [f.to_dict() for f in self.findings]}
        self.path.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    # --- 证据 ---
    def add_evidence(
        self, *, slot: str, claim: str, value: Any = None, unit: str | None = None,
        period: str | None = None, geography: str | None = None, scope: str | None = None,
        source_name: str, source_url: str | None = None, source_file: str | None = None,
        source_sha256: str | None = None, added_by: str = "agent", confidence: str = "medium",
        retrieved_at: str | None = None,
    ) -> EvidenceRecord:
        spec = SLOTS.get(slot)
        if spec is None:
            raise ValueError(f"未知证据槽位: {slot}；可用槽位: {sorted(SLOTS)}")
        if not claim.strip():
            raise ValueError("claim 不能为空")
        if not source_name.strip():
            raise ValueError("必须提供 source_name")
        if not source_url and not source_file:
            raise ValueError("证据必须有可追溯来源：source_url 或 source_file")
        if source_url and not re.match(r"^https?://", source_url):
            raise ValueError("source_url 必须以 http:// 或 https:// 开头")
        if source_file and not source_sha256:
            raise ValueError("source_file 必须附带 SHA256")
        if confidence not in CONFIDENCE:
            raise ValueError(f"confidence 必须是 {sorted(CONFIDENCE)}")
        norm_unit = normalize_unit(unit)
        parsed_value: float | str | None = value
        if spec.numeric:
            try:
                parsed_value = float(str(value).replace(",", ""))
            except (TypeError, ValueError):
                raise ValueError(f"槽位 {slot} 要求数值 value，缺失不能当作 0") from None
            if parsed_value < 0:
                raise ValueError("value 不能为负数")
        if spec.units:
            if norm_unit not in spec.units:
                hint = "原需求要求“台数”(units)，不能用 GMV/金额替代" if spec.units == ("units",) else ""
                raise ValueError(f"槽位 {slot} 的单位必须是 {list(spec.units)}，收到 {unit!r}。{hint}")
        core = [slot, claim.strip(), parsed_value, norm_unit, period, geography, scope, source_url, source_sha256]
        evidence_id = "ev:" + sha256(json.dumps(core, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:10]
        if any(e.evidence_id == evidence_id for e in self.evidence):
            raise ValueError(f"证据已存在: {evidence_id}")
        record = EvidenceRecord(
            evidence_id=evidence_id, slot=slot, claim=claim.strip(), value=parsed_value, unit=norm_unit,
            period=period, geography=geography, scope=scope, source_name=source_name.strip(),
            source_url=source_url, source_file=source_file, source_sha256=source_sha256,
            retrieved_at=retrieved_at or date.today().isoformat(), added_by=added_by, confidence=confidence,
        )
        self.evidence.append(record)
        return record

    # --- 结论 ---
    def add_finding(self, *, module: str, text: str, evidence_ids: list[str], created_at: str | None = None) -> ModuleFinding:
        modules = {s.module for s in SLOTS.values()}
        if module not in modules:
            raise ValueError(f"未知模块: {module}；可用模块: {sorted(modules)}")
        ids = list(dict.fromkeys(evidence_ids))
        if not ids:
            raise ValueError("结论必须引用至少一条证据")
        known = {e.evidence_id: e for e in self.evidence}
        missing = [i for i in ids if i not in known]
        if missing:
            raise ValueError(f"引用了不存在的证据: {missing}")
        foreign = [i for i in ids if SLOTS[known[i].slot].module != module]
        if foreign:
            raise ValueError(f"证据 {foreign} 不属于模块 {module}")
        for pattern in FORBIDDEN_DECISION_PATTERNS:
            if re.search(pattern, text, flags=re.IGNORECASE):
                raise ValueError("结论越过了人工决策边界（出现 Yes/No/进入建议类措辞）；只陈述研究发现")
        finding_id = "fd:" + sha256(json.dumps([module, text.strip(), ids], ensure_ascii=False).encode()).hexdigest()[:10]
        if any(f.finding_id == finding_id for f in self.findings):
            raise ValueError(f"结论已存在: {finding_id}")
        finding = ModuleFinding(finding_id, module, text.strip(), tuple(ids), created_at or date.today().isoformat())
        self.findings.append(finding)
        return finding

    # --- 覆盖 ---
    def coverage(self, confirmed_route_modules: list[str] | None = None) -> list[SlotStatus]:
        counts: dict[str, int] = {}
        for e in self.evidence:
            counts[e.slot] = counts.get(e.slot, 0) + 1
        return [
            SlotStatus(spec.key, spec.module, spec.title, counts.get(spec.key, 0),
                       "covered" if counts.get(spec.key, 0) else "missing", spec.provider_hint)
            for spec in required_slots(confirmed_route_modules)
        ]


def sample_share_of_market(
    records: list[BSRRecord], evidence: list[EvidenceRecord]
) -> dict[str, Any]:
    """Top 样本销量占外部美国市场规模（台数）的比例。

    仅当 (1) 存在 units 单位、年度周期的 market_size_us 证据，(2) 样本覆盖该年全部 12 个月时才计算；
    否则返回 None 并给出原因——样本销量不得直接当作美国整体市场规模。
    """
    candidates = [e for e in evidence if e.slot == "market_size_us" and e.unit == "units"
                  and e.period and re.fullmatch(r"\d{4}", e.period) and isinstance(e.value, float) and e.value > 0]
    if not candidates:
        return {"share_pct": None, "reason": "缺少年度、台数口径的美国市场规模外部证据", "evidence_id": None}
    best = sorted(candidates, key=lambda e: (e.period, e.evidence_id))[-1]
    months = {r.month for r in records if r.month.startswith(best.period)}
    if len(months) < 12:
        return {"share_pct": None, "reason": f"样本仅覆盖 {best.period} 年 {len(months)}/12 个月", "evidence_id": best.evidence_id}
    sample = sum(r.monthly_sales for r in records if r.month.startswith(best.period))
    return {"share_pct": sample / best.value * 100.0, "reason": None, "evidence_id": best.evidence_id,
            "sample_sales": sample, "market_units": best.value, "period": best.period}
