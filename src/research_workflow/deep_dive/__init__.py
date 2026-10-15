"""T2-8 证据驱动的深挖执行(纯确定性,不联网、不调模型、不出商业结论)。"""

from .engine import SUPPORTED_MODULES, THEMES, Inputs, run_deep_dive
from .models import (
    HUMAN_DECISION_SLOT,
    DeepDiveFact,
    DeepDiveReport,
    DeepDiveSection,
    EvidenceSlot,
)

__all__ = [
    "HUMAN_DECISION_SLOT", "SUPPORTED_MODULES", "THEMES", "DeepDiveFact", "DeepDiveReport",
    "DeepDiveSection", "EvidenceSlot", "Inputs", "run_deep_dive",
]
