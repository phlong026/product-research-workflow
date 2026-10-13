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

## 后续插件化，不阻塞核心 Research Workflow

- TikTok / 出海匠
- Google Trends 深挖
- 西柚关键词季节性
- SP/SB/SBV / CPC
- Reddit / Facebook VOC
- 专利深查

这些模块经 T10 人工确认后插入，不改变 T01–T11 核心数据契约。


## 2026-10-13 第一、二批整改（分支 claude/affectionate-keller-o3elk7）

已落地并有离线测试覆盖（未用真实卖家精灵/真实模型验收，请勿声称“已联调”）：

- 口径：跨父体去重（销量+评分+评分数）、逐名次销量、ASIN/品牌/卖家集中度拆分、新品留存率与推广率（见 `docs/METRIC_DEFINITIONS.md`）
- 类目节点发现/确认与同月多节点合并（节点字段别名未用真实接口验证）
- 商品补齐 `enrich` 接入主流程；`classify --provider llm`（Claude）；用量记录
- 三张过程表导出/上传（表1 上传留痕并重新审批；表2 经 enrich 上传；表3 经 resolve --edits-file）
- dossier 新增可编辑综合工作簿 `workbook.xlsx`

仍未完成：市场规模与增速的外部证据、TikTok 综合、竞争深研、Web 多人工作台等，见路线图批次 3–5。

### 第三、四批（同一分支）

- 外部证据账本（25 个槽位）、深挖结论边界、资料包“外部证据”章节；**证据需人工/AI 研究后录入，系统不自动抓取**
- 新品筛选（近 N 天、排除老商品新变体）
- 多人 Web 工作台（`docs/WORKBENCH.md`）：具名用户、项目权限、上传、持久任务队列、检查点裁决审计、下载；已用真实浏览器走通“建项目→上传→导入→确认”

### 待测试阶段确认的事项

- 卖家名在真实卖家精灵返回中的字段名（影响卖家集中度；确认前显示“未知”）
- 评分数为 0 不判重、推广阈值参考取值、表1/2/3 含义：已按推荐口径实现，真实数据对账时复核
- 是否为表2（商品补齐）增加独立的人工确认检查点（当前只有表1、表3 有）
