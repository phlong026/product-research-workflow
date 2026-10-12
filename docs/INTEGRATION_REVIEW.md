# T01–T11 全链路集成评审

## 目的

这不是新增业务功能，只验证已经独立完成的 T01–T11 可以在同一数据契约下组合运行。

## Core Workflow

```text
collect_bsr
→ 人工数据检查
→ enrich_asins
→ classify
→ 人工分类检查
→ market_metrics
→ market_analysis
→ competition_metrics
→ new_product_analysis
→ research_route
→ 人工研究路线确认
→ dossier
```

可选的 TikTok / Google Trends / 西柚关键词季节性 / 广告 / Reddit 等支线不强行塞进主干，由 T10 的人工确认路线按需调度。

## LangGraph

`build_core_research_graph()` 已将上述主干实现成 LangGraph 图工厂，并保留三个人工 interrupt：

1. 清洗数据检查
2. 产品分类检查
3. 后续研究路线检查

所有人工节点都要求明确 `approved=true` 才继续；拒绝时结束当前运行，不自动越过人工 Gate。

## Offline End-to-End Gate

`scripts/run_offline_demo.py` 不需要任何外部密钥，用合成数据真实调用：

- T03 BSR 清洗
- T04 ASIN enrichment
- T05 分类 + 人工确认模拟
- T06 市场指标
- T07 市场研究解释
- T08 竞争态势
- T09 新品 / VC
- T10 Research Router + 人工确认模拟
- T11 Research Dossier

最终必须生成完整 Dossier，且 `decision_owner=human`。

## 结论

- 模块接口已串通。
- 本地仍需验证的只剩外部运行条件：MCP SDK、SellerSprite Chrome/CDP 登录、SIF/西柚/Apify 密钥、真实 LLM structured output。
- 外部服务联调失败不会要求重写 T03–T11 业务计算逻辑。
