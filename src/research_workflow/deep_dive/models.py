from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

HUMAN_DECISION_SLOT = "人工解读与最终商业判断:由研究人员填写,系统不代填"


@dataclass(slots=True, frozen=True)
class DeepDiveFact:
    """一条可追溯的事实:数值只来自已算指标或已登记的外部证据,evidence_ids 指回出处。"""

    id: str
    label: str
    value: Any
    unit: str | None = None
    evidence_ids: tuple[str, ...] = ()
    note: str | None = None


@dataclass(slots=True, frozen=True)
class EvidenceSlot:
    """该模块需要的证据位:present=已登记,missing=尚未提供(缺失不当 0,不用默认值顶替)。"""

    kind: str
    label: str
    status: str  # present | missing
    detail: str = ""


@dataclass(slots=True, frozen=True)
class DeepDiveSection:
    module: str
    theme: str
    title: str
    facts: tuple[DeepDiveFact, ...] = ()
    slots: tuple[EvidenceSlot, ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(slots=True)
class DeepDiveReport:
    confirmed_route_ids: list[str]
    requested_modules: list[str]
    sections: list[DeepDiveSection] = field(default_factory=list)
    # 不能自动执行的模块及原因(如需要的维度当前输入没有);不是错误,须人工另行研究
    unsupported: list[dict[str, str]] = field(default_factory=list)
    human_decision_slot: str = HUMAN_DECISION_SLOT

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
