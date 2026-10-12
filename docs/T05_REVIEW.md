# T05 专项评审｜Product Classification + Human Checkpoint

## 目标

把“标题 + 卖点 → 产品形态分类 → 人工检查”固化成通用模块，不把制冰机的冰型写死。

## 开源复用

- 参考 `amazon-sorftime-research-MCP-skill` 中“属性标注 / LLM 分类 / 人工后处理”的思路。
- 不复用该仓库后续 Go/No-Go 五维评分和自动决策模板；本项目最终 Yes/No 仍由人工负责。

## 实现

- `ClassificationSchema`：每个项目定义分类维度和允许标签。
- `ClassificationProvider`：与具体 LLM SDK 解耦。
- `RuleClassificationProvider`：测试/简单规则兜底。
- `CallbackClassificationProvider`：可接任意结构化输出 LLM。
- 所有 ASIN 都必须得到分类；无证据或 provider 漏返回时归入 `unknown`。
- 人工可修改分类，修改后生成新的稳定分类版本 `cls-...`。
- 分类检查复用 T01 Human Checkpoint。

## 评审结论

- ✅ 分类 schema 通用，不绑定制冰机。
- ✅ Prompt 明确禁止进行 Yes/No 商业决策。
- ✅ 未知/无证据不会被强行分类。
- ✅ 人工修改可验证、可版本化。
- ✅ 下游只读取已确认分类版本。

## Mac 联调待办

- 接入实际 LLM structured-output callback。
- 用制冰机表2/表3抽样 20–50 ASIN 比对 AI 初分与历史人工分类。
