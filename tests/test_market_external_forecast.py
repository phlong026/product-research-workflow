import pytest

from research_workflow.market_external.forecast import (
    CoverageAssumption,
    ScopeLevel,
    SeriesPoint,
    extrapolate,
)

ANNUAL = [SeriesPoint("2024", 100.0), SeriesPoint("2025", 120.0), SeriesPoint("2026", 144.0)]


def _vals(result, name="mid"):
    return {p.period: p.value for p in result.scenarios[name]}


def test_cagr_extrapolation_hand_calc_with_scenarios():
    r = extrapolate(ANNUAL, ScopeLevel.AMAZON_CHANNEL, method="cagr", horizon_years=2,
                    scenarios={"low": 0.1, "high": 0.3}, evidence_ids=["ev1"])
    assert _vals(r)["2027"] == pytest.approx(172.8) and _vals(r)["2028"] == pytest.approx(207.36)
    assert _vals(r, "high")["2028"] == pytest.approx(243.36)
    assert _vals(r, "low")["2027"] == pytest.approx(158.4)
    assert r.is_forecast and r.base_period == "2026" and r.base_value == 144.0
    assert r.scope == ScopeLevel.AMAZON_CHANNEL and r.method == "cagr" and r.evidence_ids == ("ev1",)
    assert r.assumptions and r.to_dict()["scope"] == ScopeLevel.AMAZON_CHANNEL


def test_linear_extrapolation_hand_calc():
    r = extrapolate(ANNUAL, ScopeLevel.AMAZON_CHANNEL, method="linear", horizon_years=2,
                    scenarios={"low": 10, "high": 30})
    assert _vals(r)["2027"] == pytest.approx(144 + 22) and _vals(r)["2028"] == pytest.approx(144 + 44)
    assert _vals(r, "high")["2027"] == pytest.approx(174.0)


def test_monthly_cagr_uses_per_month_rate():
    series = [SeriesPoint("2025-01", 100.0), SeriesPoint("2026-01", 121.0)]  # 12 months, +21%
    r = extrapolate(series, ScopeLevel.AMAZON_CHANNEL, horizon_months=12, scenarios={"low": 0.1, "high": 0.3})
    assert _vals(r)["2027-01"] == pytest.approx(121 * 1.21)
    assert _vals(r, "high")["2027-01"] == pytest.approx(121 * 1.3)
    assert list(_vals(r))[0] == "2026-02"


def test_no_scenarios_means_no_interval():
    r = extrapolate(ANNUAL, ScopeLevel.AMAZON_CHANNEL, horizon_years=1)
    assert set(r.scenarios) == {"mid"} and any("interval not produced" in a for a in r.assumptions)


def test_cross_scope_without_coverage_rejected():
    for target in (ScopeLevel.AMAZON_CHANNEL, ScopeLevel.FULL_MARKET):
        with pytest.raises(ValueError, match="coverage_ratio"):
            extrapolate(ANNUAL, ScopeLevel.TOP_SAMPLE, horizon_years=1, target_scope=target)


def test_cross_scope_with_sourced_coverage_scales_values():
    cov = CoverageAssumption(0.5, "internal estimate, Jungle Scout 2026-03 report", ScopeLevel.TOP_SAMPLE,
                             ScopeLevel.AMAZON_CHANNEL)
    r = extrapolate(ANNUAL, ScopeLevel.TOP_SAMPLE, horizon_years=1, target_scope=ScopeLevel.AMAZON_CHANNEL,
                    coverage=cov)
    assert r.scope == ScopeLevel.AMAZON_CHANNEL and r.source_scope == ScopeLevel.TOP_SAMPLE
    assert r.base_value == pytest.approx(288.0) and _vals(r)["2027"] == pytest.approx(288 * 1.2)
    assert any("coverage_ratio=0.5" in a for a in r.assumptions)


def test_coverage_validation():
    with pytest.raises(ValueError, match="source"):
        CoverageAssumption(0.5, " ", ScopeLevel.TOP_SAMPLE, ScopeLevel.FULL_MARKET)
    with pytest.raises(ValueError, match="ratio"):
        CoverageAssumption(1.5, "s", ScopeLevel.TOP_SAMPLE, ScopeLevel.FULL_MARKET)
    with pytest.raises(ValueError, match="widen"):
        CoverageAssumption(0.5, "s", ScopeLevel.FULL_MARKET, ScopeLevel.TOP_SAMPLE)
    cov = CoverageAssumption(0.5, "s", ScopeLevel.TOP_SAMPLE, ScopeLevel.AMAZON_CHANNEL)
    with pytest.raises(ValueError, match="does not match"):
        extrapolate(ANNUAL, ScopeLevel.TOP_SAMPLE, horizon_years=1, target_scope=ScopeLevel.FULL_MARKET, coverage=cov)
    with pytest.raises(ValueError, match="narrow"):
        extrapolate(ANNUAL, ScopeLevel.FULL_MARKET, horizon_years=1, target_scope=ScopeLevel.TOP_SAMPLE)


def test_input_validation():
    with pytest.raises(ValueError):
        extrapolate(ANNUAL[:1], ScopeLevel.AMAZON_CHANNEL, horizon_years=1)
    with pytest.raises(ValueError, match="exactly one"):
        extrapolate(ANNUAL, ScopeLevel.AMAZON_CHANNEL)
    with pytest.raises(ValueError, match="horizon_years"):
        extrapolate(ANNUAL, ScopeLevel.AMAZON_CHANNEL, horizon_months=6)
    with pytest.raises(ValueError, match="scenario"):
        extrapolate(ANNUAL, ScopeLevel.AMAZON_CHANNEL, horizon_years=1, scenarios={"mid": 0.1})
    with pytest.raises(ValueError, match="method"):
        extrapolate(ANNUAL, ScopeLevel.AMAZON_CHANNEL, horizon_years=1, method="arima")
