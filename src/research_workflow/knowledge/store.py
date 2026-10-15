"""T5 历史知识库(R01):本地 SQLite 归档 + 检索。

只归档**已完成且分类已人工确认**的项目;检索只返回事实与历史标签供参考:
- 历史标签复用必须重新走 classification_review,系统不据此自动分类;
- 归档的「人工备注」只来自归档人输入,系统不生成、不代填任何商业结论;
- 全部参数化 SQL,库文件本地,不联网。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from research_workflow.competition_external.ledger import assert_no_decision_language

SCHEMA = """
CREATE TABLE IF NOT EXISTS archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    product TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(project_id, payload_sha256)
);
CREATE TABLE IF NOT EXISTS terms (archive_id INTEGER NOT NULL REFERENCES archives(id) ON DELETE CASCADE, term TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_terms_term ON terms(term);
CREATE TABLE IF NOT EXISTS asin_labels (
    archive_id INTEGER NOT NULL REFERENCES archives(id) ON DELETE CASCADE,
    asin TEXT NOT NULL, label TEXT NOT NULL, human_edited INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_asin_labels_asin ON asin_labels(asin);
"""

SUGGESTION_NOTE = "历史标签仅供参考:复用须在新项目 classify 之后经 classification_review 人工确认,系统不据此自动分类"
assert_no_decision_language([SUGGESTION_NOTE])

_TOKEN = re.compile(r"[a-z0-9]+|[一-鿿]+")


def tokenize(text: str) -> list[str]:
    """英文/数字按词,中文按整串 + 相邻二字组合(无分词依赖);去重保序。"""
    terms: list[str] = []
    for match in _TOKEN.findall(text.lower()):
        terms.append(match)
        if "一" <= match[0] <= "鿿" and len(match) > 2:
            terms += [match[i:i + 2] for i in range(len(match) - 1)]
    return list(dict.fromkeys(terms))


def payload_sha(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class KnowledgeBase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        return conn

    # ------------------------------------------------------------ 写
    def archive(self, payload: dict[str, Any], archived_at: str) -> dict[str, Any]:
        """同一项目同内容幂等(返回既有记录);内容变化则新增一个版本,历史版本保留。"""
        digest = payload_sha(payload)
        project = payload["project"]
        with self._connect() as conn:
            row = conn.execute("SELECT id, archived_at FROM archives WHERE project_id = ? AND payload_sha256 = ?",
                               (project["project_id"], digest)).fetchone()
            if row:
                return {"archive_id": row["id"], "status": "unchanged", "archived_at": row["archived_at"], "payload_sha256": digest}
            cursor = conn.execute(
                "INSERT INTO archives(project_id, product, marketplace, archived_at, payload_sha256, payload_json) VALUES (?,?,?,?,?,?)",
                (project["project_id"], project["product"], project["marketplace"], archived_at, digest,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True)))
            archive_id = int(cursor.lastrowid)
            terms = self._terms_of(payload)
            conn.executemany("INSERT INTO terms(archive_id, term) VALUES (?, ?)", [(archive_id, t) for t in terms])
            conn.executemany(
                "INSERT INTO asin_labels(archive_id, asin, label, human_edited) VALUES (?,?,?,?)",
                [(archive_id, item["asin"], item["label"], 1 if item["human_edited"] else 0) for item in payload["classification"]["asin_labels"]])
            version = conn.execute("SELECT COUNT(*) FROM archives WHERE project_id = ?", (project["project_id"],)).fetchone()[0]
        return {"archive_id": archive_id, "status": "archived", "archived_at": archived_at, "payload_sha256": digest, "version": version}

    @staticmethod
    def _terms_of(payload: dict[str, Any]) -> list[str]:
        project = payload["project"]
        text = " ".join([project["product"], project.get("primary_keyword") or "", project.get("main_node") or "", project["marketplace"],
                         *[f"{label['key']} {label['name']} {label.get('description', '')}" for label in payload["classification"]["labels"]],
                         *payload.get("human_notes", [])])
        return tokenize(text)

    # ------------------------------------------------------------ 读
    def _latest(self, conn: sqlite3.Connection, marketplace: str | None) -> list[sqlite3.Row]:
        """每个项目只取最新版本。"""
        sql = ("SELECT a.* FROM archives a WHERE a.id = (SELECT MAX(id) FROM archives WHERE project_id = a.project_id)"
               + (" AND a.marketplace = ?" if marketplace else "") + " ORDER BY a.archived_at DESC, a.project_id")
        return conn.execute(sql, (marketplace,) if marketplace else ()).fetchall()

    def search(self, query: str, *, marketplace: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        terms = tokenize(query)
        if not terms:
            raise ValueError("检索词为空")
        with self._connect() as conn:
            latest = {row["id"]: row for row in self._latest(conn, marketplace)}
            if not latest:
                return []
            marks = ",".join("?" for _ in terms)
            hits = conn.execute(
                f"SELECT archive_id, COUNT(DISTINCT term) AS matched FROM terms WHERE term IN ({marks}) GROUP BY archive_id", terms).fetchall()
            scored = [(row["matched"], latest[row["archive_id"]]) for row in hits if row["archive_id"] in latest]
        scored = sorted(scored, key=lambda item: (-item[0], _neg(item[1]["archived_at"]), item[1]["project_id"]))
        results = []
        for matched, row in scored[:limit]:
            payload = json.loads(row["payload_json"])
            results.append({
                "archive_id": row["id"], "project_id": row["project_id"], "product": row["product"], "marketplace": row["marketplace"],
                "archived_at": row["archived_at"], "matched_terms": matched, "query_terms": len(terms),
                "labels": [label["name"] for label in payload["classification"]["labels"]],
                "asin_count": len(payload["classification"]["asin_labels"]),
                "human_notes": payload.get("human_notes", []),
            })
        return results

    def get(self, *, archive_id: int | None = None, project_id: str | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            if archive_id is not None:
                row = conn.execute("SELECT * FROM archives WHERE id = ?", (archive_id,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM archives WHERE project_id = ? ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
        if row is None:
            return None
        return {"archive_id": row["id"], "archived_at": row["archived_at"], "payload_sha256": row["payload_sha256"],
                "payload": json.loads(row["payload_json"])}

    def label_suggestions(self, query: str, *, marketplace: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        out = []
        for hit in self.search(query, marketplace=marketplace, limit=limit):
            record = self.get(archive_id=hit["archive_id"])
            assert record is not None
            counts: dict[str, int] = {}
            for item in record["payload"]["classification"]["asin_labels"]:
                counts[item["label"]] = counts.get(item["label"], 0) + 1
            out.append({
                "from_project": hit["project_id"], "product": hit["product"], "marketplace": hit["marketplace"],
                "archived_at": hit["archived_at"], "dimension": record["payload"]["classification"]["dimension"],
                "labels": [{**label, "asin_count": counts.get(label["key"], 0)} for label in record["payload"]["classification"]["labels"]],
                "note": SUGGESTION_NOTE,
            })
        return out

    def lookup_asins(self, asins: list[str]) -> dict[str, list[dict[str, Any]]]:
        wanted = [a.strip().upper() for a in asins if a.strip()]
        if not wanted:
            raise ValueError("ASIN 为空")
        found: dict[str, list[dict[str, Any]]] = {asin: [] for asin in wanted}
        marks = ",".join("?" for _ in wanted)
        with self._connect() as conn:
            latest_ids = {row["id"] for row in self._latest(conn, None)}
            rows = conn.execute(
                f"SELECT l.asin, l.label, l.human_edited, a.id AS archive_id, a.project_id, a.product, a.archived_at "
                f"FROM asin_labels l JOIN archives a ON a.id = l.archive_id WHERE l.asin IN ({marks}) "
                f"ORDER BY a.archived_at DESC, a.project_id", wanted).fetchall()
        for row in rows:
            if row["archive_id"] in latest_ids:
                found[row["asin"]].append({"project_id": row["project_id"], "product": row["product"], "label": row["label"],
                                           "human_edited": bool(row["human_edited"]), "archived_at": row["archived_at"]})
        return found


def _neg(text: str) -> tuple[int, ...]:
    """让 archived_at 降序可以放进升序 key(ISO 时间字符串逐字符取反)。"""
    return tuple(-ord(ch) for ch in text)
