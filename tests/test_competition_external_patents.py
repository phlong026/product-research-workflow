from __future__ import annotations

import hashlib
import json

import pytest

from research_workflow.competition_external.ledger import (
    EvidenceRef,
    assert_no_decision_language,
    evidence_from_row,
)
from research_workflow.competition_external.patents import (
    PatentRecord,
    PatentSearchLog,
    load_patent_records,
    load_patent_search_logs,
    summarize_patent_ledger,
)

BANNED_KEYS = ("infring", "legal_conclusion", "侵权", "risk", "verdict")


def _all_keys(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key)
            yield from _all_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _all_keys(item)


def _evidence():
    return EvidenceRef("USPTO", "2026-10-01", url="https://example.com/p/1")


def _record(**kw):
    base = dict(patent_no="US123", title="Ice maker", evidence=_evidence(), related_asin="B0AAA")
    base.update(kw)
    return PatentRecord(**base)


def test_not_searched_differs_from_searched_none():
    none = PatentSearchLog("ice maker", "USPTO", "US utility 2015-2026", "searched_none", "2026-10-01")
    unsearched = PatentSearchLog("ice maker", "USPTO", "US utility 2015-2026", "not_searched")
    assert none.to_dict()["status"] != unsearched.to_dict()["status"]
    assert none.to_dict()["status_label"] == "已检索-范围内未发现"
    assert unsearched.to_dict()["status_label"] == "未检索"
    summary = summarize_patent_ledger([none, unsearched])
    assert summary["status_counts"] == {"searched_found": 0, "searched_none": 1, "not_searched": 1}


def test_status_consistency_is_enforced():
    with pytest.raises(ValueError):
        PatentSearchLog("q", "USPTO", "s", "searched_found", "2026-10-01")  # found without records
    with pytest.raises(ValueError):
        PatentSearchLog("q", "USPTO", "s", "searched_none", None)  # searched without a time
    with pytest.raises(ValueError):
        PatentSearchLog("q", "USPTO", "s", "searched_none", "2026-10-01", (_record(),))
    with pytest.raises(ValueError):
        PatentSearchLog("q", "USPTO", "s", "maybe", "2026-10-01")


def test_output_has_no_legal_conclusion_fields_and_has_disclaimer():
    found = PatentSearchLog("ice maker", "USPTO", "US", "searched_found", "2026-10-01", (_record(),))
    summary = summarize_patent_ledger([found])
    keys = [k.lower() for k in _all_keys(summary)]
    for banned in BANNED_KEYS:
        assert not any(banned in key for key in keys), banned
    assert "不构成法律意见" in summary["disclaimer"]
    assert "不构成法律意见" in summary["searches"][0]["disclaimer"]


def test_record_requires_related_target():
    with pytest.raises(ValueError):
        PatentRecord(patent_no="US1", title="t", evidence=_evidence())


def test_import_csv_and_missing_source_raises(tmp_path):
    good = tmp_path / "patents.csv"
    good.write_text(
        "patent_no,title,related_asin,source_name,source_url,filed_date,retrieved_at,search_query\n"
        "US1,Ice maker,B0AAA,USPTO,https://example.com/1,2020-01-02,2026-10-01,ice maker\n",
        encoding="utf-8",
    )
    records = load_patent_records(good)
    assert records[0].patent_no == "US1"
    assert records[0].evidence.url == "https://example.com/1"

    logs_file = tmp_path / "logs.csv"
    logs_file.write_text(
        "query,database,scope,status,searched_at\n"
        "ice maker,USPTO,US,searched_found,2026-10-01\n"
        "nugget,USPTO,US,not_searched,\n",
        encoding="utf-8",
    )
    logs = load_patent_search_logs(logs_file, records)
    assert logs[0].records == tuple(records)
    assert logs[1].status == "not_searched"

    bad = tmp_path / "bad.csv"
    bad.write_text(
        "patent_no,title,related_asin,source_name,source_url,retrieved_at\n"
        "US1,Ice maker,B0AAA,,https://example.com/1,2026-10-01\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="source_name"):
        load_patent_records(bad)


def test_import_json_records(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "patent_no": "US2",
                        "title": "t",
                        "related_product": "ice maker",
                        "source_name": "Google Patents",
                        "source_url": "https://example.com/2",
                        "retrieved_at": "2026-10-02",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert load_patent_records(path)[0].related_product == "ice maker"


def test_screenshot_sha256_verification(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-image-bytes")
    digest = hashlib.sha256(b"fake-image-bytes").hexdigest()

    ref = EvidenceRef.from_file("Helium10", shot, "2026-10-01")
    assert ref.sha256 == digest
    assert ref.verify() == "match"

    EvidenceRef("Helium10", "2026-10-01", file_path=str(shot), sha256=digest)  # no error
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        EvidenceRef("Helium10", "2026-10-01", file_path=str(shot), sha256="0" * 64)

    shot.write_bytes(b"tampered")  # file changed after the hash was recorded
    assert ref.verify() == "mismatch"


def test_evidence_requires_source_and_locator():
    with pytest.raises(ValueError):
        EvidenceRef("", "2026-10-01", url="https://x")
    with pytest.raises(ValueError):
        EvidenceRef("src", "2026-10-01")
    with pytest.raises(ValueError, match="row 3"):
        evidence_from_row({"retrieved_at": "2026-10-01", "url": "u"}, row_no=3)


def test_forbidden_language_guard():
    assert_no_decision_language(["仅为检索记录,不构成法律意见"])
    for text in ("存在侵权风险", "建议进入", "YES", "go/no-go"):
        with pytest.raises(ValueError):
            assert_no_decision_language([text])
