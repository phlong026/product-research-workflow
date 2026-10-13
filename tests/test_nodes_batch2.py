"""节点发现与多节点合并。"""
from __future__ import annotations

import json

import pytest

from research_workflow.data import discover_nodes, merge_node_rows
from scripts.run_research import main


def test_discover_nodes_proposes_primary_and_related():
    rows = (
        [{"asin": f"A{i}", "monthly_sales": 100, "nodeIdPath": "N1", "nodeLabelPath": "Ice Makers"} for i in range(8)]
        + [{"asin": f"B{i}", "monthly_sales": 30, "nodeIdPath": "N2", "nodeLabelPath": "Portable"} for i in range(3)]
        + [{"asin": "C1", "monthly_sales": 1, "nodeIdPath": "N3"}]
        + [{"asin": "D1", "monthly_sales": 50}]
    )
    p = discover_nodes(rows)
    assert p.primary.node_id == "N1" and p.primary.label == "Ice Makers"
    assert [c.node_id for c in p.related] == ["N2"]
    assert [c.node_id for c in p.others] == ["N3"]
    assert p.rows_without_node == 1 and p.sampled_rows == 13


def test_discover_nodes_without_node_fields_proposes_nothing():
    p = discover_nodes([{"asin": "A", "monthly_sales": 5}])
    assert p.primary is None and p.rows_without_node == 1


def test_merge_node_rows_dedupes_asin_across_nodes_and_keeps_node_ids():
    merged = merge_node_rows({
        "N1": [{"asin": "A", "monthly_sales": 10}, {"asin": "B", "monthly_sales": 5}],
        "N2": [{"asin": "A", "monthly_sales": 12}, {"asin": "C", "monthly_sales": 7}],
    })
    by = {r["asin"]: r for r in merged}
    assert set(by) == {"A", "B", "C"}
    assert by["A"]["monthly_sales"] == 12 and by["A"]["_node_ids"] == ["N1", "N2"]


@pytest.fixture()
def run(tmp_path, capsys):
    def _run(*argv):
        capsys.readouterr()
        main(["--db", str(tmp_path / "db.sqlite"), "--root", str(tmp_path / "projects"), *argv])
        return json.loads(capsys.readouterr().out)

    def _fail(*argv):
        capsys.readouterr()
        with pytest.raises(SystemExit):
            main(["--db", str(tmp_path / "db.sqlite"), "--root", str(tmp_path / "projects"), *argv])
        return json.loads(capsys.readouterr().out)["error"]

    _run.fail = _fail
    return _run


def _csv(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_two_nodes_same_month_merge_instead_of_overwrite(run, tmp_path):
    pid = run("init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    n1 = _csv(tmp_path, "2026-01-n1.csv", "ASIN,月销量,品牌,商品标题,小类BSR\nA,100,X,a,1\nB,50,Y,b,2\n")
    n2 = _csv(tmp_path, "2026-01-n2.csv", "ASIN,月销量,品牌,商品标题,小类BSR\nB,60,Y,b,1\nC,40,Z,c,2\n")
    run("ingest", "--project", pid, "--file", n1, "--month", "2026-01", "--node", "N1")
    out = run("ingest", "--project", pid, "--file", n2, "--month", "2026-01", "--node", "N2")
    assert out["months"]["2026-01"]["output_rows"] == 3
    pdir = tmp_path / "projects" / pid
    records = json.loads((pdir / "records.json").read_text(encoding="utf-8"))["2026-01"]
    assert {r["asin"]: r["monthly_sales"] for r in records} == {"A": 100, "B": 60, "C": 40}
    assert {r["asin"]: r["node_ids"] for r in records}["B"] == ["N1", "N2"]
    sources = json.loads((pdir / "sources.json").read_text(encoding="utf-8"))
    assert {v["node"] for v in sources.values()} == {"N1", "N2"}
    # 同月同节点重导入只替换该节点
    n1b = _csv(tmp_path, "2026-01-n1b.csv", "ASIN,月销量,品牌,商品标题,小类BSR\nA,10,X,a,1\n")
    out = run("ingest", "--project", pid, "--file", n1b, "--month", "2026-01", "--node", "N1")
    assert out["months"]["2026-01"]["output_rows"] == 3  # A、B(来自N2)、C
    sources = json.loads((pdir / "sources.json").read_text(encoding="utf-8"))
    assert len(sources) == 2


def test_cannot_mix_node_and_no_node_for_same_month(run, tmp_path):
    pid = run("init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    f = _csv(tmp_path, "2026-01.csv", "ASIN,月销量\nA,10\n")
    run("ingest", "--project", pid, "--file", f, "--month", "2026-01", "--node", "N1")
    assert "混用" in run.fail("ingest", "--project", pid, "--file", f, "--month", "2026-01")


def test_node_discover_confirm_and_enforcement(run, tmp_path):
    pid = run("init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    sample = _csv(tmp_path, "sample.csv",
                  "ASIN,月销量,nodeIdPath,nodeLabelPath\nA,100,N1,Ice\nB,90,N1,Ice\nC,40,N2,Portable\n")
    out = run("nodes-discover", "--project", pid, "--file", sample)
    assert out["proposal"]["primary"]["node_id"] == "N1"
    assert "不在发现结果" in run.fail("nodes-confirm", "--project", pid, "--node", "N9")
    run("nodes-confirm", "--project", pid, "--node", "N1", "--node", "N2")
    f = _csv(tmp_path, "2026-01.csv", "ASIN,月销量\nA,10\n")
    assert "未经人工确认" in run.fail("ingest", "--project", pid, "--file", f, "--month", "2026-01", "--node", "N7")


def test_merge_prefers_valid_zero_over_invalid_sales():
    merged = merge_node_rows({
        "N1": [{"asin": "A", "monthly_sales": "n/a"}],
        "N2": [{"asin": "A", "monthly_sales": 0}],
    })
    assert merged[0]["monthly_sales"] == 0 and merged[0]["_node_ids"] == ["N1", "N2"]
    merged = merge_node_rows({"N1": [{"asin": "A", "monthly_sales": 0}], "N2": [{"asin": "A", "monthly_sales": "n/a"}]})
    assert merged[0]["monthly_sales"] == 0
