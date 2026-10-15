"""关键词 / 使用场景 / 用户评论(VOC)外部证据(T6)。

离线纯库:调用方把人工(或外部 AI)整理好的文件交进来,本包只做确定性汇总。
- 主题/情感/场景/细分标签一律来自输入文件,系统不做语义判断,也不调用模型;
- 缺值不当 0(无搜索量的行不进份额分母,并如实计数);
- 每次导入必须带来源名、取数日期、取数范围(scope),链接或文件哈希至少一项可追溯;
- 系统生成的文案过禁词守卫:只陈述事实,不含商业结论。
"""

from .evidence import (
    SENTIMENTS,
    KeywordRow,
    ScenarioRow,
    VocRow,
    load_keyword_rows,
    load_scenario_rows,
    load_voc_rows,
    summarize_keywords,
    summarize_scenarios,
    summarize_voc,
)

__all__ = [
    "SENTIMENTS", "KeywordRow", "ScenarioRow", "VocRow",
    "load_keyword_rows", "load_scenario_rows", "load_voc_rows",
    "summarize_keywords", "summarize_scenarios", "summarize_voc",
]
