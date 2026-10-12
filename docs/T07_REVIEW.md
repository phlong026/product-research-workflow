# T07 专项评审｜Market Research Agent

## 范围

T07 只解释 T06 已计算的结构化指标：

- 市场同比/环比现象
- 细分与大盘走势差异
- 份额迁移
- 需要继续研究的问题

## 开源复用

- 参考开源 Amazon product-research Skill 的“结构化数据 → LLM 分析 → 报告”模式。
- 不复用其自动决策、五维评分和 Go/No-Go Prompt。

## 防幻觉设计

1. T06 指标转换为稳定 `metric_id`。
2. LLM 每条 finding 必须引用真实 `evidence_ids`；不存在的引用直接丢弃。
3. LLM 不重新计算原始数据。
4. 未被指标证明的原因只能进入 `follow_up_questions`，不能写成事实。
5. 输出出现 `YES/NO/建议进入/不建议进入/最终决策` 等商业裁决内容时直接拒绝。

## 无 LLM 时

内置 deterministic research summary 可直接运行：例如识别“大盘下降、细分增长”的走势背离，并提出后续研究问题。因此核心 Workflow 不会被某个模型供应商阻塞。

## 评审结论

- ✅ 数字计算与自然语言解释分离。
- ✅ 每条 AI finding 有结构化证据引用。
- ✅ 未证实因果不会被包装成事实。
- ✅ 最终 Yes/No 仍由人工完成。
