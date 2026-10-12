"""Skill CLI (scripts/run_research.py) 端到端集成测试。

覆盖 AI 助手驱动的完整协议:init → ingest → [数据检查点·含驳回重导]
→ classify → [分类检查点·含人工修改] → analyze → [路线检查点·含多选确认] → dossier。
同时锁定红线:报告的人工决策栏必须留空。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_research import main

CSV_2025 = """ASIN,月销量,品牌,商品标题,小类BSR,卖家所属地
N1,20,NBrand,Nugget Ice Maker Countertop,1,CN
B1,80,BBrand,Bullet Ice Maker for Home,2,US
"""

CSV_2026 = """ASIN,月销量,品牌,商品标题,小类BSR,卖家所属地
N1,25,NBrand,Nugget Ice Maker Countertop,1,CN
B1,55,BBrand,Bullet Ice Maker for Home,2,US
"""


class CLI:
    def __init__(self, tmp_path: Path, capsys) -> None:
        self.tmp_path = tmp_path
        self.capsys = capsys
        self.base = [
            "--db",
            str(tmp_path / "research.db"),
            "--root",
            str(tmp_path / "projects"),
        ]

    def run(self, *argv: str) -> dict:
        self.capsys.readouterr()
        main([*self.base, *argv])
        return json.loads(self.capsys.readouterr().out)


@pytest.fixture()
def cli(tmp_path, capsys) -> CLI:
    return CLI(tmp_path, capsys)


def _write(tmp_path: Path, name: str, content: str) -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_full_skill_workflow_with_checkpoints(cli: CLI, tmp_path: Path) -> None:
    file_2025 = _write(tmp_path, "2025-01.csv", CSV_2025)
    file_2026 = _write(tmp_path, "2026-01.csv", CSV_2026)

    # 1. init
    out = cli.run("init", "--product", "Ice Maker", "--marketplace", "US")
    project_id = out["project_id"]
    assert out["next"] == "ingest"

    # 2. ingest → 数据检查点
    out = cli.run(
        "ingest",
        "--project",
        project_id,
        "--file",
        file_2025,
        "--file",
        file_2026,
    )
    assert set(out["months"]) == {"2025-01", "2026-01"}
    assert out["checkpoint_step"] == "data_review"
    data_cp = out["checkpoint_id"]
    assert (Path(out["records_file"])).exists()

    # 3. 驳回数据检查点 → 重新导入生成新检查点
    out = cli.run(
        "resolve", "--project", project_id, "--checkpoint", data_cp, "--reject"
    )
    assert out["resolution"] == "rejected"
    out = cli.run("ingest", "--project", project_id, "--file", file_2025, "--file", file_2026)
    data_cp2 = out["checkpoint_id"]
    assert data_cp2 != data_cp
    out = cli.run("resolve", "--project", project_id, "--checkpoint", data_cp2, "--approve")
    assert out["next"] == "classify"

    # 4. classify:只给 nugget 规则 → B1 落 unknown,等待人工修改
    out = cli.run(
        "classify",
        "--project",
        project_id,
        "--label",
        "nugget=咀嚼冰",
        "--label",
        "bullet=子弹冰",
        "--rule",
        "nugget=nugget",
    )
    assert out["label_counts"] == {"nugget": 1, "unknown": 1}
    cls_cp = out["checkpoint_id"]

    # 5. 人工修改 B1=bullet 后批准
    out = cli.run(
        "resolve",
        "--project",
        project_id,
        "--checkpoint",
        cls_cp,
        "--approve",
        "--edit",
        "B1=bullet",
    )
    assert out["edits"] == {"B1": "bullet"}
    assert out["next"] == "analyze"

    # 6. analyze:大盘下降 + nugget 逆势增长 → 两条研究分支
    out = cli.run("analyze", "--project", project_id, "--pair", "2025-01:2026-01")
    assert out["latest_market"]["yoy_pct"] == pytest.approx(-20.0)
    route_ids = {item["route_id"] for item in out["route_suggestions"]}
    assert "market_decline_deep_dive" in route_ids
    assert "segment_migration:nugget" in route_ids
    route_cp = out["checkpoint_id"]

    # 7. 多选确认研究分支(含带冒号的 route_id)
    out = cli.run(
        "resolve",
        "--project",
        project_id,
        "--checkpoint",
        route_cp,
        "--approve",
        "--select",
        "market_decline_deep_dive,segment_migration:nugget",
    )
    assert out["selected_routes"] == ["market_decline_deep_dive", "segment_migration:nugget"]

    # 8. dossier
    dossier_dir = tmp_path / "dossier-out"
    out = cli.run("dossier", "--project", project_id, "--output", str(dossier_dir))
    assert out["project_status"] == "completed"
    files = set(out["files"])
    assert {"report.md", "dashboard.html", "data.json", "manifest.json", "csv/market.csv"} <= files

    report = (dossier_dir / "report.md").read_text(encoding="utf-8")
    assert "人工最终判断" in report and "□ YES" in report
    assert "已人工确认" in report  # 路线确认状态进入报告

    # 9. status:研究完成,无待裁决检查点
    out = cli.run("status", "--project", project_id)
    assert out["project"]["status"] == "completed"
    assert out["pending_checkpoint"] is None
    assert out["next"] == "done"


def test_cli_guards_and_status_recovery(cli: CLI, tmp_path: Path) -> None:
    file_2025 = _write(tmp_path, "2025-01.csv", CSV_2025)
    out = cli.run("init", "--product", "Ice Maker", "--marketplace", "US")
    project_id = out["project_id"]

    # analyze 前置条件:必须先有分类且已确认
    with pytest.raises(SystemExit):
        cli.run("analyze", "--project", project_id)

    # status 在仅建项目后给出下一步指引
    out = cli.run("status", "--project", project_id)
    assert out["next"] == "ingest"

    out = cli.run("ingest", "--project", project_id, "--file", file_2025)
    out = cli.run("status", "--project", project_id)
    assert out["pending_checkpoint"]["step_key"] == "data_review"
    assert out["next"] == "resolve"

    # resolve 必须二选一
    with pytest.raises(SystemExit):
        cli.run(
            "resolve",
            "--project",
            project_id,
            "--checkpoint",
            out["pending_checkpoint"]["checkpoint_id"],
        )

    # 无 --edit 的批准也必须把分类置为 approved(回归:此前 batch 原样写回导致 approved=False)
    data_cp = out["pending_checkpoint"]["checkpoint_id"]
    cli.run("resolve", "--project", project_id, "--checkpoint", data_cp, "--approve")
    out = cli.run(
        "classify",
        "--project",
        project_id,
        "--label",
        "nugget=咀嚼冰",
        "--label",
        "bullet=子弹冰",
        "--rule",
        "nugget=nugget",
        "--rule",
        "bullet=bullet",
    )
    cli.run("resolve", "--project", project_id, "--checkpoint", out["checkpoint_id"], "--approve")
    envelope = json.loads(
        (tmp_path / "projects" / project_id / "classification.json").read_text(encoding="utf-8")
    )
    assert envelope["batch"]["approved"] is True
