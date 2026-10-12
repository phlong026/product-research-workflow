# Development Status

T01–T11 纯代码主干已经开发完成。当前最终门禁：**67 tests passed + compileall PASS + offline end-to-end PASS**。

| Task | 状态 | 已完成的核心输出 | 仍需 Mac / 真实环境 |
|---|---|---|---|
| T00 | ✅ | Ice Maker Golden Case manifest / baseline | 用客户原始 ZIP 做最终文件级复核 |
| T01 | ✅ | Project / Run / Step state、checkpoint、pause/resume、idempotent resume、retry run | 安装 LangGraph 后做真实 interrupt/resume 联调 |
| T02 | ✅ | SellerSprite/SIF/西柚/Apify MCP adapters；SellerSprite 业务接口、分页/重试/解包 | 登录 SellerSprite、注入密钥、真实 `list_tools/call_tool` |
| T03 | ✅ | Monthly BSR parse、父体/标题去重、TopN、seller region、数据质量报告 | 用真实 SellerSprite 文件/API 做字段映射验收 |
| T04 | ✅ | Vendor-neutral ASIN enrichment + primary/fallback merge | 实连 `asin_detail` / SIF fallback |
| T05 | ✅ | 通用 Classification Schema、provider interface、人工修改/冻结版本 | 接真实 structured-output LLM，与历史制冰机分类抽查 |
| T06 | ✅ | 月度市场/细分/地区、MoM/YoY、份额分解、跨期窗口比较 | 用 Golden Case 重算 2025/2026 Jan–Apr 等冻结指标 |
| T07 | ✅ | Evidence-grounded market analysis、metric citation、Yes/No guardrail | 接真实 LLM 做输出稳定性验收 |
| T08 | ✅ | Top3/10 ASIN/品牌、累计 BSR 层级、头/腰/尾 band、虹吸信号 | 用 Golden Case 重算集中度/BSR 基线 |
| T09 | ✅ | 新品 launch success / current survival、VC/SC、地区/细分汇总、缺月未知态 | 用 2024–2026 新品表重算 895/195/21.8%/80% |
| T10 | ✅ | Human-confirmed Research Router + manual fallback | 前端实现研究支线多选确认 |
| T11 | ✅ | JSON/CSV/Markdown/HTML Dossier + manifest；人工决策留空 | 浏览器看 ECharts、接真实来源索引 |

## 后续插件化，不阻塞核心 Research Workflow

- TikTok / 出海匠
- Google Trends 深挖
- 西柚关键词季节性
- SP/SB/SBV / CPC
- Reddit / Facebook VOC
- 专利深查

这些模块经 T10 人工确认后插入，不改变 T01–T11 核心数据契约。
