# Development Status

2026-10-12：定位为受控单机研究试点。最新整改、测试与真实验收缺口见 [上线评审](docs/RELEASE_READINESS.md) 和 [测试证据](docs/release-readiness-20261012/RESULTS.md)。下表为模块级实现清单，✅ 不代表真实外部服务验收通过。

| Task | 状态 | 已完成的核心输出 | 仍需 Mac / 真实环境 |
|---|---|---|---|
| T00 | ✅ | Ice Maker Golden Case manifest / baseline | 用客户原始 ZIP 做最终文件级复核 |
| T01 | ✅ | Project / Run / Step state、checkpoint、pause/resume、idempotent resume、retry run | 离线 LangGraph interrupt/resume 已测；持久执行器与跨进程图恢复未实现 |
| T02 | ✅ | SellerSprite/SIF/西柚/Apify MCP adapters；SellerSprite 业务接口、分页/重试/解包 | 登录 SellerSprite、注入密钥、真实 `list_tools/call_tool` |
| T03 | ✅ | Monthly BSR parse、父体/标题去重、TopN、seller region、数据质量报告 | 用真实 SellerSprite 文件/API 做字段映射验收 |
| T04 | ✅ | Vendor-neutral ASIN enrichment + primary/fallback merge | 实连 `asin_detail` / SIF fallback |
| T05 | ✅ | 通用 Classification Schema、provider interface、人工修改/冻结版本 | 接真实 structured-output LLM，与历史制冰机分类抽查 |
| T06 | ✅ | 月度市场/细分/地区、MoM/YoY、份额分解、跨期窗口比较 | 用 Golden Case 重算 2025/2026 Jan–Apr 等冻结指标 |
| T07 | ✅ | Evidence-grounded market analysis、metric citation、Yes/No guardrail | 接真实 LLM 做输出稳定性验收 |
| T08 | ✅ | Top3/10 ASIN/品牌、累计 BSR 层级、头/腰/尾 band、虹吸信号 | 用 Golden Case 重算集中度/BSR 基线 |
| T09 | ✅ | 新品 launch success / current survival、VC/SC、地区/细分汇总、缺月未知态 | 用 2024–2026 新品表重算 895/195/21.8%/80% |
| T10 | ✅ | Human-confirmed Research Router + manual fallback | 前端实现研究支线多选确认 |
| T11 | ✅ | JSON/CSV/Markdown/离线SVG HTML Dossier + manifest；人工决策留空 | 离线SVG图表与输入/审批证据已导出；真实业务来源仍待验收 |

## 差距整改进度(依据《跨境选品开发任务清单》,分支 feat/phase1-data-chain)

> ✅=代码与离线测试完成;🔌=已接入 CLI;**均未经真实供应商联调与业务样本对账**。口径见 [口径确认单](docs/口径确认单.md)。

| 任务 | 状态 | 说明 |
|---|---|---|
| T1-1 节点发现 | ✅🔌 | `discovery/`、`discover-nodes`;节点字段名为推断 |
| T1-2 节点参数/节点×月存储 | ✅🔌 | `ingest --live --nodes`,`raw/<月>__<节点>.json`,多节点按 ASIN 合并 |
| T1-3 月份窗口/Top200 | ✅🔌 | `--months auto`、`--candidate-n`,缺月写 `coverage.json` |
| T1-4/T1-5 去重与排名 | ✅🔌 | 三值跨父体规则 + 审计表;`sales_rank` 连续名次 |
| T1-6/T1-7 地区/卖点补齐 | ✅🔌 | `classify --enrich` → `enrichment.json` |
| T1-8 GPT 分类 | ✅🔌 | `classify --provider llm`(待人工拦截);真实模型输出未验收 |
| T1-9 三表人工校正协议 | ⏳ | 数据/分类检查点已绑定哈希;表级导出-上传-失效协议未做 |
| T2-1/T2-2/T2-4 | ✅🔌 | 透视、国别同比环比、季节性、产品迭代进程 |
| T2-3/T2-5/T2-6/T2-7 | 🚧 | `market_external/` 进行中(导入器+确定性计算,不联网) |
| T2-8 三类深挖执行 | ⏳ | 未开始 |
| T3-1/T3-2 | ✅🔌 | 卖家口径集中度、单点名次矩阵 |
| T3-3/T3-4/T3-5 | ✅🔌 | 新品队列、原文口径留存率、双阈值 |
| T3-6~T3-9 | 🚧 | `competition_external/` 进行中(证据账本,不联网) |
| T4-1~T4-3 执行器/Web/多人 | ⏳ | 未开始 |
| T4-4 综合 Excel | ✅🔌 | `research_workbook.xlsx` |
| T5/T6 | ⏳ | 未开始 |

## 后续插件化，不阻塞核心 Research Workflow

- TikTok / 出海匠
- Google Trends 深挖
- 西柚关键词季节性
- SP/SB/SBV / CPC
- Reddit / Facebook VOC
- 专利深查

这些模块经 T10 人工确认后插入，不改变 T01–T11 核心数据契约。
