import json

import pytest

from research_workflow.market_external.tiktok import (
    analyze_tiktok,
    cagr_2024_2026,
    load_units_file,
    parse_units_rows,
    project_2026_2028,
    tiktok_share,
)


def _row(period, units, channel="tiktok", geo="us", segment="countertop", fc=False, ev=None):
    return {"geo": geo, "segment": segment, "channel": channel, "period": period, "units": units,
            "is_forecast": fc, "evidence_id": ev or f"ev:{channel}:{period}"}


def _records(extra=()):
    rows = [
        _row("2024", 100), _row("2026", 144, fc=True),
        _row("2024", 1000, "all"), _row("2026", 1200, "all", fc=True),
        *extra,
    ]
    return parse_units_rows(rows)


def test_gmv_field_rejected_in_rows_and_csv(tmp_path):
    with pytest.raises(ValueError, match="GMV"):
        parse_units_rows([{**_row("2024", 1), "tiktok_GMV": 5}])
    p = tmp_path / "u.csv"
    p.write_text("geo,segment,channel,period,units,is_forecast,evidence_id,gmv_usd\n"
                 "us,total,all,2024,1,false,e,9\n", encoding="utf-8")
    with pytest.raises(ValueError, match="GMV"):
        load_units_file(p)


def test_unit_label_must_be_units_and_is_forecast_explicit():
    with pytest.raises(ValueError, match="unit"):
        parse_units_rows([{**_row("2024", 1), "unit": "USD"}])
    row = _row("2024", 1)
    del row["is_forecast"]
    with pytest.raises(ValueError, match="is_forecast"):
        parse_units_rows([row])
    with pytest.raises(ValueError, match="evidence_id"):
        parse_units_rows([{**_row("2024", 1), "evidence_id": ""}])


def test_json_import(tmp_path):
    p = tmp_path / "u.json"
    p.write_text(json.dumps({"records": [_row("2024", 100)]}), encoding="utf-8")
    assert load_units_file(p)[0].units == 100.0


def test_share_hand_calc_and_missing():
    recs = _records()
    s = tiktok_share(recs, "us", "countertop", "2024")
    assert s.share == pytest.approx(0.1) and s.evidence_ids == ("ev:tiktok:2024", "ev:all:2024")
    assert tiktok_share(recs, "us", "countertop", "2026").is_forecast is True
    assert tiktok_share(recs, "global", "countertop", "2024").status == "insufficient_evidence"


def test_cagr_hand_calc():
    r = cagr_2024_2026(_records(), "us", "countertop")
    assert r.status == "ok" and r.cagr == pytest.approx(0.2)  # (144/100)**0.5 - 1
    assert r.is_forecast is True and "^(1/2)" in r.method


def test_projection_hand_calc_with_scenarios_and_flags():
    res = project_2026_2028(_records(), "us", "countertop", high_growth=0.3, low_growth=0.1)
    got = {(p.scenario, p.year): p.units for p in res.points}
    assert got[("mid", 2027)] == pytest.approx(172.8)
    assert got[("mid", 2028)] == pytest.approx(207.36)
    assert got[("high", 2027)] == pytest.approx(187.2) and got[("high", 2028)] == pytest.approx(243.36)
    assert got[("low", 2027)] == pytest.approx(158.4) and got[("low", 2028)] == pytest.approx(174.24)
    assert res.is_forecast and all(p.is_forecast for p in res.points)
    assert res.method == "cagr_extrapolation" and res.base_year == 2026 and res.assumptions
    assert "ev:tiktok:2024" in res.evidence_ids


def test_projection_without_scenarios_has_no_interval_and_bad_order_rejected():
    res = project_2026_2028(_records(), "us", "countertop")
    assert {p.scenario for p in res.points} == {"mid"}
    assert any("interval not produced" in a for a in res.assumptions)
    with pytest.raises(ValueError, match="low <= mid"):
        project_2026_2028(_records(), "us", "countertop", high_growth=0.1, low_growth=0.3)


def test_missing_evidence_gives_no_conclusion():
    recs = parse_units_rows([_row("2026", 144)])  # no 2024 base
    res = project_2026_2028(recs, "us", "countertop", high_growth=0.3, low_growth=0.1)
    assert res.status == "insufficient_evidence" and res.points == ()
    assert cagr_2024_2026(recs, "us", "countertop").cagr is None


def test_monthly_2026_marks_forecast_and_missing_months():
    rows = [_row(f"2026-{m:02d}", 10 * m, fc=m > 3) for m in range(1, 12)]  # December absent
    analysis = analyze_tiktok(parse_units_rows(rows), geo="us", segment="countertop")
    flags = {p.month: p.is_forecast for p in analysis.monthly_2026}
    assert flags["2026-03"] is False and flags["2026-04"] is True
    assert analysis.missing_months_2026 == ("2026-12",)
    assert analysis.annual == ()  # 11 months must not be summed into an annual number


def test_annual_from_twelve_months_and_analysis_to_dict():
    rows = [_row(f"2026-{m:02d}", 10, fc=m > 6) for m in range(1, 13)] + [_row("2024", 60)]
    analysis = analyze_tiktok(parse_units_rows(rows), geo="us", segment="countertop")
    assert dict(analysis.annual)[2026].units == 120 and dict(analysis.annual)[2026].is_forecast
    assert analysis.cagr_2024_2026.cagr == pytest.approx((120 / 60) ** 0.5 - 1)
    assert analysis.to_dict()["unit"] == "units"
