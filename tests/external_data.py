"""外部证据接线测试共用的合成数据与 CLI 驱动(只含虚构数字,不联网)。"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path

import pytest

from scripts.run_research import main

MONTH_HEADER = "ASIN,月销量,品牌,商品标题,小类BSR,卖家所属地\n"
TRENDS_META = ["geo=US", "timeframe=2025-01-01 2026-02-28", "source_name=Google Trends",
               "source_url=https://trends.google.com/trends/explore", "as_of=2026-03-01"]
TIKTOK_META = ["source_name=Vendor Report", "source_url=file://vendor_units.csv", "as_of=2026-02-01",
               "low_growth=0.1", "high_growth=0.3"]
BRAND_META = ["brand=Acme", "product_type=ice maker", "aba_rank=15000", "source_name=Helium10",
              "retrieved_at=2026-10-01", "url=https://example.com/h10"]
CROSS_META = ["source_name=Helium10", "retrieved_at=2026-10-01", "brand=Acme"]

TRENDS_CSV = "date,keyword,value\n" + "".join(
    f"{2025 + i // 12}-{i % 12 + 1:02d}-01,ice maker,{50 + i}\n" for i in range(14))
EXCERPTS_CSV = (
    "claim_id,excerpt,source_name,date,url,status\n"
    "rep:1,US countertop ice maker units 2025 reached 1.2 million,Statista,2026-01-15,https://example.com/r1,present\n"
    "rep:2,,Paywalled Report,2026-01-20,https://example.com/r2,inaccessible\n"
)
EXCERPTS_CSV_2 = (
    "claim_id,excerpt,source_name,date,url,status\n"
    "rep:3,Second report excerpt,Mintel,2026-02-01,https://example.com/r3,present\n"
)
EXCERPTS_NO_CLAIM_ID = "excerpt,source_name,date,url\nUnnamed excerpt,Statista,2026-01-15,https://example.com/r9\n"
EXCERPTS_NO_SOURCE = "claim_id,excerpt,source_name,date,url\nrep:9,Orphan number 42,,2026-01-15,\n"
TIKTOK_CSV = (
    "geo,segment,channel,period,units,is_forecast,evidence_id\n"
    "us,countertop,tiktok,2024,100,false,tt:tiktok:2024\n"
    "us,countertop,tiktok,2026,144,true,tt:tiktok:2026\n"
    "us,countertop,all,2024,1000,false,tt:all:2024\n"
    "us,countertop,all,2026,1200,true,tt:all:2026\n"
)
TIKTOK_GMV_CSV = (
    "geo,segment,channel,period,units,is_forecast,evidence_id,gmv_usd\n"
    "us,countertop,tiktok,2024,100,false,tt:tiktok:2024,9000\n"
)
PATENTS_CSV = (
    "patent_no,title,related_asin,source_name,retrieved_at,url,search_query\n"
    "US1234567,Ice maker with nugget tray,N1,USPTO,2026-10-01,https://example.com/p1,ice maker nugget\n"
)
PATENT_LOGS_CSV = (
    "query,database,scope,status,searched_at\n"
    "ice maker nugget,USPTO,US utility 2015-2026,searched_found,2026-10-01\n"
    "ice maker bullet,USPTO,US utility 2015-2026,searched_none,2026-10-01\n"
    "ice maker clear,USPTO,US utility 2015-2026,not_searched,\n"
)
ADS_JSON = [{
    "asin": "N1", "cohort": "head_waist_tail", "selected_by": "analyst-A",
    "source_name": "Helium10", "retrieved_at": "2026-10-01", "url": "https://example.com/h10",
    "traffic_time_range": "2026-07..2026-09", "traffic_data": {"organic": 0.6, "sponsored": 0.4},
    "reviews_time_range": "2025-10..2026-09",
    "review_months": [{"month": f"2025-{m:02d}", "new_reviews": 10, "sales": 100} for m in range(1, 13)],
    "cpc_time_range": "2026-07..2026-09", "cpc_value": 1.25, "cpc_currency": "USD",
}]
BRAND_ASINS_CSV = (
    "asin,listed_date,rating,monthly_sales\n"
    "B0AAA,2022-03-01,4.5,300\nB0BBB,2023-06-01,4.3,\n"
)
OFFSITE_CSV = (
    "brand,product_term,query,engine_tool,searched_at,status,channel_type,url,discovered_at\n"
    "Acme,ice maker,Acme ice maker review,Google,2026-10-01,found,social,https://example.com/post,2026-10-01\n"
    "Acme,ice maker,Acme ice maker deal,Bing,2026-10-01,not_found,,,\n"
    "Acme,ice maker,Acme ice maker coupon,Google,,not_searched,,,\n"
)
CROSS_CATEGORY_CSV = (
    "asin,category,listed_date,monthly_sales\n"
    "C1,kitchen,2024-01-10,120\nC2,kitchen,2025-05-10,80\nC3,garden,2025-07-01,\n"
)

# kind -> (文件名, 内容, meta)
SAMPLES: dict[str, tuple[str, str, list[str]]] = {
    "trends": ("trends.csv", TRENDS_CSV, TRENDS_META),
    "report-excerpts": ("excerpts.csv", EXCERPTS_CSV, []),
    "tiktok-units": ("tiktok_units.csv", TIKTOK_CSV, TIKTOK_META),
    "patents": ("patents.csv", PATENTS_CSV, []),
    "patent-search-log": ("patent_logs.csv", PATENT_LOGS_CSV, []),
    "ads": ("ads.json", json.dumps(ADS_JSON), []),
    "big-brand": ("brand_asins.csv", BRAND_ASINS_CSV, BRAND_META),
    "offsite": ("offsite.csv", OFFSITE_CSV, []),
    "cross-category": ("cross_category.csv", CROSS_CATEGORY_CSV, CROSS_META),
}
ALL_KINDS = list(SAMPLES)


def meta_args(meta: list[str]) -> list[str]:
    return [item for entry in meta for item in ("--meta", entry)]


class Cli:
    def __init__(self, tmp_path: Path, capsys) -> None:
        self.tmp_path = tmp_path
        self.capsys = capsys
        self.base = ["--db", str(tmp_path / "db.sqlite"), "--root", str(tmp_path / "projects")]

    def run(self, *argv: str) -> dict:
        self.capsys.readouterr()
        main([*self.base, *argv])
        return json.loads(self.capsys.readouterr().out)

    def fail(self, *argv: str) -> dict:
        """期望命令以错误退出,返回错误 JSON。"""
        self.capsys.readouterr()
        with pytest.raises(SystemExit) as info:
            main([*self.base, *argv])
        assert info.value.code == 1
        return json.loads(self.capsys.readouterr().out)

    def pdir(self, pid: str) -> Path:
        return self.tmp_path / "projects" / pid

    def write(self, name: str, content: str) -> Path:
        path = self.tmp_path / "samples" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def import_file(self, pid: str, kind: str, path: Path, meta: list[str]) -> dict:
        return self.run("import-evidence", "--project", pid, "--kind", kind, "--file", str(path), *meta_args(meta))

    def import_sample(self, pid: str, kind: str) -> dict:
        name, content, meta = SAMPLES[kind]
        return self.import_file(pid, kind, self.write(name, content), meta)


class QuietCli(Cli):
    """不依赖 capsys 的驱动,供 module 级 fixture 使用。"""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path, None)

    def run(self, *argv: str) -> dict:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main([*self.base, *argv])
        return json.loads(buffer.getvalue())


def tree_digest(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def analyzed_project(cli: Cli, *, months: int = 24, confirm_route: bool = False) -> str:
    """24 个月(2025-01..2026-12)合成数据走到 analyze;confirm_route=True 时再确认路线,可直接 dossier。"""
    files: list[str] = []
    for i in range(months):
        month = f"{2025 + i // 12}-{i % 12 + 1:02d}"
        path = cli.tmp_path / "months" / f"{month}.csv"
        path.parent.mkdir(exist_ok=True)
        path.write_text(MONTH_HEADER + f"N1,{100 + 5 * i},NBrand,Nugget Ice Maker,1,CN\n"
                        f"B1,{50 + 2 * i},BBrand,Bullet Ice Maker,2,US\n", encoding="utf-8")
        files += ["--file", str(path)]
    pid = cli.run("init", "--product", "Ice Maker", "--marketplace", "US")["project_id"]
    out = cli.run("ingest", "--project", pid, *files)
    cli.run("resolve", "--project", pid, "--checkpoint", out["checkpoint_id"], "--approve")
    out = cli.run("classify", "--project", pid, "--label", "nugget=Nugget", "--label", "bullet=Bullet",
                  "--rule", "nugget=nugget", "--rule", "bullet=bullet")
    cli.run("resolve", "--project", pid, "--checkpoint", out["checkpoint_id"], "--approve")
    out = cli.run("analyze", "--project", pid)
    if confirm_route:
        cli.run("resolve", "--project", pid, "--checkpoint", out["checkpoint_id"], "--approve")
    return pid
