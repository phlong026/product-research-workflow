"""深挖研究(T2-8)在资料包中的呈现:每个模块一张事实表 + 证据缺口,不含商业结论。"""
from __future__ import annotations

from typing import Any

from research_workflow.competition_external.ledger import assert_no_decision_language
from research_workflow.deep_dive import HUMAN_DECISION_SLOT

from . import sections

TITLE = "深挖研究(证据驱动)"
NOT_RUN = "本次运行未执行深挖(未运行 deep-dive)。"
INTRO_NOTES = (
    "本节只汇总已算指标与已登记的外部证据,每条事实标注证据 ID;不设内置阈值,不判断好坏或是否进入。",
    "「缺失」表示证据尚未提供,不当 0、不用默认值顶替;替代关系、增长原因等解读由人工完成。",
)
assert_no_decision_language([TITLE, NOT_RUN, *INTRO_NOTES, HUMAN_DECISION_SLOT])

FACT_HEADERS = ("事实", "数值/描述", "单位", "证据 ID", "备注")
SLOT_HEADERS = ("所需证据", "状态", "说明")
_STATUS = {"present": "已登记", "missing": "缺失"}


def deep_dive_blocks(doc: dict[str, Any]) -> list[sections.TableBlock]:
    report = doc["report"]
    blocks: list[sections.TableBlock] = []
    for section in report["sections"]:
        facts = tuple((f["label"], str(f["value"]), f["unit"] or "-", "、".join(f["evidence_ids"]) or "-", f["note"] or "-")
                      for f in section["facts"])
        note = f"主题:{section['theme']};" + ";".join(section["limitations"])
        blocks.append(sections.TableBlock(f"{section['title']}（{section['module']}）", FACT_HEADERS,
                                          facts or (("无可汇总的事实", "-", "-", "-", "-"),), note))
        blocks.append(sections.TableBlock(f"{section['title']}:证据状态", SLOT_HEADERS,
                                          tuple((s["label"], _STATUS[s["status"]], s["detail"] or "-") for s in section["slots"])))
    if report["unsupported"]:
        blocks.append(sections.TableBlock("无法自动执行的模块", ("模块", "原因"),
                                          tuple((item["module"], item["reason"]) for item in report["unsupported"])))
    return blocks


def deep_dive_markdown(doc: dict[str, Any] | None) -> list[str]:
    lines = ["", f"## {TITLE}", ""]
    if doc is None:
        return [*lines, NOT_RUN]
    lines += [f"- {note}" for note in INTRO_NOTES]
    lines.append(f"- 已确认路线:{'、'.join(doc['report']['confirmed_route_ids']) or '无(使用显式 --modules)'}")
    for block in deep_dive_blocks(doc):
        lines.append("")
        lines += sections.markdown_table(block)
    lines += ["", f"- {HUMAN_DECISION_SLOT}"]
    return lines
