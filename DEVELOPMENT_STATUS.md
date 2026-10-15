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
| T1-6/T1-7 地区/卖点补齐 | ✅🔌 | 独立 `enrich` 命令 → `enrichment.json`;**补齐检查点 `enrichment_review`**(表2 人工确认关口:未确认不能分类/分析/出资料包);`import-table --table 2` 重新请求该检查点;`classify --enrich` 已废弃 |
| T1-8 GPT 分类 | ✅🔌 | `classify --provider llm`(待人工拦截);真实模型输出未验收 |
| T1-9 三表人工校正协议 | ✅🔌 | `export-table`/`import-table`:校验失败不改状态,成功后旧审批失效并重新请求确认;Web 同步支持 |
| T2-1/T2-2/T2-4 | ✅🔌 | 透视、国别同比环比、季节性、产品迭代进程 |
| T2-3/T2-5/T2-6/T2-7 | ✅🔌 | 库+接入已完成,待真实数据:`market_external/` 经 `import-evidence` / `build-market-forecast` / `build-cross-channel` 接入 CLI 与 dossier(「外部证据与预测」章节、`外部_*` 工作表);未经真实供应商数据验收 |
| T2-8 深挖执行 | ✅🔌 | `deep-dive`:按人工确认路线(或显式 `--modules`)汇总已算指标+已登记证据,逐项列出证据缺口,不设阈值、不出结论;`deep_dive.json` 绑定输入哈希,过期则 dossier 拒绝;dossier 新增「深挖研究」章节与 `深挖_汇总` 工作表;Web 有作业入口(无专用页面)。**模块范围/口径待业务确认(见口径确认单 #9)** |
| T3-1/T3-2 | ✅🔌 | 卖家口径集中度、单点名次矩阵 |
| T3-3/T3-4/T3-5 | ✅🔌 | 新品队列、原文口径留存率、双阈值 |
| T3-6~T3-9 | ✅🔌 | 库+接入已完成,待真实数据:`competition_external/` 经 `import-evidence` 接入 CLI 与 dossier;证据登记不是检查点,`external/` 导出前校验索引哈希 |
| T4-1~T4-3 执行器/Web/多人 | ✅🔌 | `python -m research_workflow.web`:子进程作业执行器(失败可重试、重启恢复)、账号/项目隔离/CSRF、单页前端；已过一轮独立安全评审并修复,**上线前须人工安全评审**；单进程部署；未经多人真实并发验收 |
| T4-4 综合 Excel | ✅🔌 | `research_workbook.xlsx` |
| 验收对账 `reconcile` | ✅🔌 | 指标与人工结果逐项对账(缺失判 MISSING 而非 0);**对账本身尚未用真实业务样本执行** |
| 新品阈值无内置默认 | ✅🔌 | 推起/留存阈值缺任一项则不计算新品指标并披露原因(库层 `NewProductConfig` 仍保留 250 供单元测试) |
| T5 历史知识库(R01) | ✅🔌(本地版) | `kb-archive/kb-search/kb-labels/kb-lookup/kb-show`:SQLite 归档已完成项目的分类体系、逐 ASIN 标签、指标摘要、阈值、审批、证据索引与人工备注;检索/历史标签只作参考,复用须重新经 classification_review。**未做向量语义检索;不对 Web 开放** |
| T6 关键词/场景/VOC | ✅🔌(证据登记版) | `import-evidence --kind keywords|scenarios|voc`:登记并确定性汇总(scope 必填、缺值不当 0),接入 dossier 与 deep-dive 的 `keyword_research/scenario_research/voc_research`。**系统不自动抓取评论/搜索量,标签由输入提供** |
| 客户安装包(Windows) | ✅(Linux 上验证) | `python packaging/build_customer_package.py` → `dist/xuanpin-research-<版本>-windows.zip`(+ .sha256):`install.bat/ps1`(装 uv、Python 3.12、按锁文件装依赖)、`start.bat`(只监听本机)、`check.bat`、客户 README/操作手册/给 Codex 的安装说明。已在 Linux 上把解压后的包完整跑通(uv sync→安装配置→启动→登录→建项目→上传→三个检查点→资料包下载)。**`.bat`/`.ps1` 本身没有在 Windows 上执行过**(此环境无 Windows/PowerShell),首次在客户机上请先小范围试装 |
| T6 飞书机器人(R02) | ⏳ | 未开始(需要飞书应用凭据与外网放行,当前无法联调) |

## 后续插件化，不阻塞核心 Research Workflow

- TikTok / 出海匠
- Google Trends 深挖
- 西柚关键词季节性
- SP/SB/SBV / CPC
- Reddit / Facebook VOC
- 专利深查

这些模块经 T10 人工确认后插入，不改变 T01–T11 核心数据契约。


## 截至 2026-10-15:开发并未全部完成

代码侧已完成并通过离线测试的部分见上表;**以下仍未完成**:

1. **真实环境验收一项都没做**:卖家精灵联调(字段名/卖家 ID/节点字段均为推断;云环境网络策略当前拦截了各数据源域名,需放行后才能联调)、真实模型分类质量与费用、客户原始样本(制冰机)逐项对账、Windows/Mac 部署验收、多人真实并发。
2. **T2-8/T5/T6 是离线版**:深挖模块只汇总已有指标与人工登记证据,没有用真实外部数据跑过;模块范围待业务确认(口径确认单 #9);知识库没有向量检索;关键词/场景/VOC 只做登记与汇总,不自动抓取。
3. **外部证据靠人工/AI 录入**:市场规模与增速、TikTok 台数、专利、广告/CPC、大品牌、站外渠道、关键词/场景/VOC 均需 `import-evidence`;系统不自动抓取。
4. **R02 飞书机器人未开始**。
5. **待业务/测试阶段确认的口径**:卖家 ID 真实字段名;评分数为 0 不判重(研发推荐,待对账复核);深挖模块范围(#9);知识库归档范围(#10)。
6. **Web 工作台**:单进程部署;上线前须人工安全评审;未经多人真实并发验收;外部证据与深挖只有作业入口,没有专用页面。
