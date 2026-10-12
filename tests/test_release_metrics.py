import asyncio
import pytest

from research_workflow.data import build_monthly_top, MonthlyBSRConfig
from research_workflow.data.models import BSRRecord
from research_workflow.metrics import build_market_metrics
from research_workflow.new_products import analyze_new_products, NewProductConfig, NewProductSnapshot
from research_workflow.enrichment.adapters import profile_from_payload
from research_workflow.analysis.market import _parse_llm_report


def test_non_ascii_titles_are_not_deduplicated():
    result = build_monthly_top([{'asin':'A','title':'制冰机','monthly_sales':10},
                               {'asin':'B','title':'咖啡机','monthly_sales':20}], '2026-01')
    assert len(result.records) == 2


@pytest.mark.parametrize('bad', ['', 'NaN', 'Infinity', '-1', '1.5'])
def test_invalid_sales_are_reported_and_excluded(bad):
    result = build_monthly_top([{'asin':'A','monthly_sales':bad}], '2026-01')
    assert result.quality.invalid_sales_rows == 1
    assert not result.records


@pytest.mark.parametrize('month', ['2026-13', '../outside', '2026-01/../../outside'])
def test_bsr_rejects_invalid_months(month):
    with pytest.raises(ValueError):
        build_monthly_top([], month)


def test_top_n_must_be_positive():
    with pytest.raises(ValueError):
        build_monthly_top([], '2026-01', MonthlyBSRConfig(top_n=-1))


def test_incomplete_period_does_not_claim_decline():
    records = [BSRRecord('A', f'{year}-{month:02}', 100)
               for year, count in [(2025,4),(2026,3)] for month in range(1,count+1)]
    result = build_market_metrics(records, {}, period_comparison_pairs=[('2025-01','2025-04','2026-01','2026-04')])
    row = result.period_comparisons[0]
    assert row.change_pct is None
    assert row.missing_current_months == ('2026-04',)


def test_unknown_survival_not_counted_as_failure():
    result = analyze_new_products([NewProductSnapshot('A','2026-01',300),
                                  NewProductSnapshot('B','2026-01',300),
                                  NewProductSnapshot('B','2026-02',300)], NewProductConfig(consecutive_months=1))
    assert result.summary.survival_rate_among_successful_pct == 100
    assert result.summary.current_observation_coverage_pct == 50
    assert result.summary.unknown_successful_products == 1


def test_mismatched_asin_is_not_assigned_to_requested_product():
    assert profile_from_payload({'asin':'B','title':'wrong'}, asin='A', source='fake') is None
    assert profile_from_payload({'data':[{'asin':'B','title':'wrong'}]}, asin='A', source='fake') is None


def test_mixed_valid_and_fabricated_evidence_drops_entire_finding():
    report = _parse_llm_report({'findings':[{'title':'x','detail':'x','evidence_ids':['valid','fake']}]},
                               {'valid':{}}, provider='mock')
    assert not report.findings


def test_brand_case_variants_share_concentration_group():
    from research_workflow.competition import build_competition_metrics
    rows = [BSRRecord(str(i),'2026-01',sales,brand=brand) for i,(brand,sales) in enumerate([('Acme',30),('ACME',30),('acme',30),('Other',10)])]
    assert build_competition_metrics(rows).concentration[0].top3_brand_share_pct == 100
