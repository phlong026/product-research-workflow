# T01–T11 Final Code Audit

## Result

- Core Python tests: **67 passed**
- Python compileall: **PASS**
- Offline end-to-end demo: **PASS**
- Secret scan: **PASS** (no committed live key/token detected)
- Scope guardrail: **PASS** (system does not produce final commercial Yes/No)
- Git working tree: expected clean after this audit commit

## Completion fixes added after module-by-module review

### T01
- Persisted `run_id`, `thread_id`, graph version, run state and step state.
- Added resume idempotency key and retry-as-new-run semantics.
- Failed runs keep previous artifact references instead of silently discarding them.

### T02
- SellerSprite business-level adapter now exposes product research, ASIN detail, competitor batch lookup and reviews.
- Added bounded pagination, retry handling and response-envelope normalization.
- Verified operation names/limits against the open-source SellerSprite operation catalog (`product_research`, `competitor_lookup`, `asin_detail`, `review`).
- SIF / 西柚 / Apify remain replaceable MCP adapters; secrets are environment-only.

### T03
- Added input data-quality report: missing ASIN, invalid sales, missing title/brand/rank, unknown seller region, dedupe counts.

### T06
- Added configurable period-to-period comparison, including cases such as Jan–Apr 2025 vs Jan–Apr 2026.
- Supports both total market and segment sales/share change.

### T08
- Kept cumulative BSR layers (Top1/5/10/20/30/50/70/100).
- Added non-cumulative rank bands so head / waist / tail can be compared without double counting.

### T09
- Split `launch_success` from `current_survival`.
- Missing latest-month observation is now **unknown**, not falsely converted to zero sales / dead product.
- Added account type, seller-region and segment success summaries.

### T10
- Added `manual_research` fallback when no standard branch is triggered.
- Router still only selects *what to research next*, never whether to launch the product.

### T11
- Dossier now includes data completeness, missing evidence, period comparisons, seller-region structure, BSR bands, new-product structure and source index.
- Final Yes/No area is a blank human form only.

## What still requires Mac / real credentials

These are integration checks, not missing business code:

1. Install project extras: `.[dev,workflow,data]`.
2. Start/log into SellerSprite Chromium/CDP and run real MCP `list_tools/call_tool`.
3. Inject SIF / 西柚 / Apify credentials through `.env` or deployment secrets.
4. Run real field-mapping acceptance against the customer SellerSprite/SIF/西柚 responses.
5. Connect a structured-output LLM for T05/T07 and check against the historical Ice Maker classification/report.
6. Recompute the frozen Ice Maker business baseline from the original customer workbooks.
7. Open the generated HTML dashboard in a browser and confirm ECharts rendering.

## Deliberately not implemented in core T01–T11

The following stay plugin modules because the original workflow treats them as follow-up research and the Ice Maker package did not fully validate every chain:

- TikTok / 出海匠 deep research
- Google Trends deep-dive
- 西柚 keyword seasonality
- SP/SB/SBV and CPC research
- Reddit / Facebook VOC
- Patent deep search

They can be attached after T10 route confirmation without changing the core contracts.
