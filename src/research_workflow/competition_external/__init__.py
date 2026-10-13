"""External competition evidence ledgers (patents R21, ads R23, big brand R24, off-site R28).

Offline by design: callers pass data in (objects, CSV or JSON). Every record carries its
source, date, link/file reference and search scope. No business or legal verdicts are produced.
"""

from .ads import (
    COHORTS,
    AdRecord,
    CpcRecord,
    MonthlyReviewSales,
    ReviewRateResult,
    ReviewSeries,
    TrafficStructure,
    compute_review_rate_12m,
    load_ad_records,
)
from .big_brand import (
    DEFAULT_ABA_RANK_THRESHOLD,
    BigBrandCheck,
    BrandAsin,
    check_big_brand,
    load_brand_asins,
)
from .ledger import (
    EvidenceRef,
    assert_no_decision_language,
    evidence_from_row,
    sha256_of_file,
)
from .offsite import (
    CrossCategoryAsin,
    OffsiteFinding,
    OffsiteLedger,
    OffsiteSearch,
    load_cross_category_asins,
    load_offsite_searches,
    summarize_cross_category,
)
from .patents import (
    PATENT_DISCLAIMER,
    PatentRecord,
    PatentSearchLog,
    load_patent_records,
    load_patent_search_logs,
    summarize_patent_ledger,
)

__all__ = [
    "COHORTS",
    "DEFAULT_ABA_RANK_THRESHOLD",
    "PATENT_DISCLAIMER",
    "AdRecord",
    "BigBrandCheck",
    "BrandAsin",
    "CpcRecord",
    "CrossCategoryAsin",
    "EvidenceRef",
    "MonthlyReviewSales",
    "OffsiteFinding",
    "OffsiteLedger",
    "OffsiteSearch",
    "PatentRecord",
    "PatentSearchLog",
    "ReviewRateResult",
    "ReviewSeries",
    "TrafficStructure",
    "assert_no_decision_language",
    "check_big_brand",
    "compute_review_rate_12m",
    "evidence_from_row",
    "load_ad_records",
    "load_brand_asins",
    "load_cross_category_asins",
    "load_offsite_searches",
    "load_patent_records",
    "load_patent_search_logs",
    "sha256_of_file",
    "summarize_cross_category",
    "summarize_patent_ledger",
]
