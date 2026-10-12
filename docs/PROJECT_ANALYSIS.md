# 项目详细分析报告（合并版）

> 本文档由 `product-research-workflow-t00` 与 `product-research-workflow-dev` 合并后整理。
> 合并结果:`product-research-workflow/`(即原 dev 目录全量,经逐文件比对确认它已包含 t00 的全部内容且为演进版本)。

## 一、项目定位

**Amazon 跨境选品「研究工作流引擎」**——围绕"人工才是最终决策者"这一原则构建的确定性研究流水线。

系统自动完成:数据获取 → 清洗 → 产品分类 → 市场指标计算 → 竞争分析 → 新品分析 → 研究路线建议 → 研究资料包(Dossier)生成。**最终的商业 Yes/No 判断永远由人工做出**,这条红线通过代码级的 guardrail 强制执行(详见 §四.3)。

### 核心设计哲学(README 与代码一致)

```text
MCP / Connector          ← 数据获取(SellerSprite / SIF / 西柚 / Apify)
   ↓
Normalized Data          ← 归一化数据
   ↓
Deterministic Python     ← 所有可确定的计算用纯 Python 完成,LLM 不负责算数
   ↓
LLM classification / explanation  ← LLM 只做分类、解释、聚类
   ↓
Human checkpoints        ← 三个人工检查点(LangGraph interrupt)
   ↓
Research Dossier         ← 可移植的研究资料包(JSON/CSV/MD/HTML)
   ↓
Human business decision  ← 人工商业决策(系统输出物中刻意留空)
```

## 二、架构分层与模块清单

代码总量约 5000 行(src + scripts),按 T00–T11 任务切片,每片独立可测:

| 层 | 模块 | 对应任务 | 职责 |
|---|---|---|---|
| 基础 | `mcp/` | T00 | MCP 注册表/网关(厂商中立),密钥仅从环境变量注入 |
| 基础 | `golden_case/` | T00 | 制冰机案例文件级 SHA256 manifest + 冻结基线指标 |
| 领域 | `domain/` | T01 | Project / Run / Step / HumanCheckpoint 数据模型与状态枚举 |
| 领域 | `store/sqlite.py` | T01 | SQLite 持久化(项目/运行/步骤/检查点/工作流状态) |
| 领域 | `workflow/` | T01 | WorkflowService(项目与运行生命周期)+ LangGraph 主图 |
| 数据 | `connectors/` | T02 | SellerSprite 业务适配器、通用 MCP 适配器、能力路由表 |
| 数据 | `data/` | T03 | 月度 BSR 清洗:字段别名映射、父体/标题去重、TopN、数据质量报告 |
| 数据 | `enrichment/` | T04 | ASIN 补全:主源+回退源合并、并发控制、完整性判断 |
| 智力 | `classification/` | T05 | 通用分类 Schema、Provider 协议、人工修改与版本冻结 |
| 智力 | `metrics/` | T06 | 市场指标:月度/细分/地区、MoM/YoY、份额分解、跨期比较 |
| 智力 | `analysis/` | T07 | 证据约束的市场研究分析(证据引用 + 决策边界守卫) |
| 智力 | `competition/` | T08 | 集中度(Top3/10 ASIN/品牌)、BSR 累积层级、头/腰/尾带、虹吸信号 |
| 智力 | `new_products/` | T09 | 新品 launch success / current survival、VC/SC、地区/细分汇总 |
| 智力 | `router/` | T10 | 人工确认的研究路由器(建议下一个研究分支,绝不给商业结论) |
| 输出 | `dossier/` | T11 | JSON/CSV/Markdown/HTML 四格式资料包 + 文件级 manifest |
| 输出 | `api/` | T01 | FastAPI 服务(项目/运行/检查点的 REST 接口) |

## 三、合并说明(t00 → 合并版)

逐文件比对结论:

1. **t00 的全部文件都被 dev 包含**(无任何 t00 独有文件)。
2. 5 个共有文件存在差异,dev 侧全部是**演进后的更优版本**:
   - `.env.example`:dev 修正了 SIF 认证头(`secret-key` 头替代想当然的 `Bearer`);
   - `mcp/client.py`:dev 修复了 `list_tools` **分页 bug**(处理 `next_cursor`,t00 版本在工具多于首页时会漏);
   - `.gitignore`:dev 补充了 `research_workflow.db`、`*.db-journal`;
   - `pyproject.toml`:dev 的 pytest `pythonpath` 加了 `"."`;
   - `README.md`:dev 是覆盖 T00–T11 的完整版。
3. 因此合并 = 以 dev 为基准建立 `product-research-workflow/`,t00 无需保留任何补丁。

**合并后验证**:
- `pytest`:**67 passed**(0.43s)
- 离线端到端 demo:`scripts/run_offline_demo.py` 成功产出 report.md / dashboard.html / 13 张 CSV / data.json / source_index.json / manifest.json
- 环境:Python 3.12(uv venv),依赖 `.[dev,workflow,data]`(mcp / langgraph / fastapi / uvicorn / polars / fastexcel / pytest)

## 四、关键实现细节

### 4.1 工作流与人工检查点(T01,LangGraph)

`workflow/full_graph.py:build_core_research_graph()` 定义 14 阶段固定主干:

```
collect_bsr → [data_review] → enrich_asins → classify → [classification_review]
→ apply_classification_review → market_metrics → market_analysis
→ competition_metrics → new_product_analysis → research_route → [route_review]
→ apply_route_review → dossier → END
```

- 方括号三处是**人工检查点**,用 LangGraph 的 `interrupt()` 实现(HITL);人在 `human_response` 中回 `{"approved": true/false}`,`_approval_edge` 决定继续或终止。
- 重表(数据表)不进 LangGraph state,state 只携带 `data_refs`/`metrics_refs` 等工件引用,避免状态膨胀。
- `WorkflowService` 提供:项目/运行创建、启动、暂停(`WAITING_HUMAN`)、幂等恢复(resume key)、**失败/暂停后以新 run 重试**(`retry_of` 链)、检查点创建/裁决。状态机含 draft/running/paused/completed/failed 五态,步骤状态含 `waiting_human`。
- SQLite 存储(382 行,参数化 SQL)持久化项目、运行、步骤、检查点与 JSON 状态;FastAPI 暴露 `/projects`、`/runs`、`/checkpoints` 等 REST 接口。

### 4.2 数据获取与清洗(T02/T03)

**T02 连接器**(`connectors/`):
- `ToolGateway` Protocol + `MCPGateway`(薄封装官方 MCP Python SDK v2,list_tools 已做分页处理);
- `SellerSpriteConnector` 是**业务语义适配器**:业务代码调 `product_research()` / `asin_detail()` / `competitor_lookup()` / `reviews()`,不感知 MCP 工具名和原始响应结构;
- 分页显式有界(SellerSprite 单页上限 100、总量约 2000;评论单页 10);带 `with_retries`(指数退避,零依赖自实现);
- `_extract_rows_and_total()` 兼容多种响应信封(`data/result/payload` × `items/list/rows/records/content`),不把厂商格式泄漏到上游;
- `catalog.py` 内置**能力路由表**:如 `amazon_product_research → sellersprite(主)/sif(备)`,`traffic_and_ads → xiyou(主)/sif(备)`,`public_web_research → apify`。

**T03 BSR 清洗**(`data/bsr_pipeline.py`):
- 字段别名表(`月销量/销量/monthly_sales` 等)兼容中英文导出表头;
- 两级去重:先按父 ASIN(变体归并),再按 `品牌+归一化标题`(防蹭标题);同键保留"销量高→排名好→ASIN 小"的代表行;
- 卖家地区归一化:`CN(HK)/HK/HONGKONG → CN`,`USA → US`,空白→`UNKNOWN`;
- 输出 TopN(默认 100)并附 **PipelineStats**(每级行数)+ **DataQualityReport**(缺 ASIN/销量非法/缺标题/缺品牌/缺排名/地区未知/两级去重删除数)。

### 4.3 「LLM 不算数、系统不决策」的三重防线

这是整个项目最核心的工程约束:

1. **计算与解释分离**(T06/T07):市场指标全部由确定性 Python 计算;`analysis/market.py` 把已算好的指标构建成 `evidence` 索引(如 `market:2026-01`、`decomp:2025-01:2026-01:nugget`),LLM 的 prompt 明确要求"只解释、不复算、不决策"。LLM 返回的 finding 若 `evidence_ids` 无法验证则**整条丢弃**,不进报告。
2. **决策边界守卫**(guardrail):`_assert_no_business_decision()` 用正则(`\bYES\b`、`建议进入`、`go/no-go` 等)扫描 LLM 输出,越界直接抛异常;`tests/test_scope_guardrail.py` 锁死该行为。
3. **Dossier 留空**:`report.md` 第 8 节"人工最终判断(系统不自动填写)"只有空复选框;T10 Router 只回答"下一步研究什么",从不回答"要不要做"。

### 4.4 指标体系(T06/T08/T09)

**T06 市场指标**(`metrics/market.py`):
- 月度大盘:总销量、ASIN 数、MoM%、YoY%;
- 细分(segment,来自 T05 分类)× 地区(seller_region)月度:销量、份额(销量/ASIN 两个口径);
- **精确两因子份额分解**:`M1*S1 - M0*S0 = (M1-M0)*S0 + M1*(S1-S0)`,把细分销量变化拆成"大盘水涨"贡献 + "份额提升"贡献,避免近似误差;
- 跨期窗口比较(可配置,如 2025 Jan–Apr vs 2026 Jan–Apr)。

**T08 竞争**(`competition/metrics.py`):
- 集中度:Top3/Top10 ASIN 与品牌销量份额(缺品牌的 ASIN 用 `<unknown:ASIN>` 隔离,避免假品牌归并);
- BSR 累积层级(Top1/5/10/20/30/50/70/100 销量与占比)——排名缺失的行排在有排名行之后;
- 非累积头/腰/尾带(1–5 / 6–30 / 31–100),避免重复计数;
- **虹吸信号**:头层(Top10)同比增长而宽层(Top100)下降 → `head_up_broad_down`,识别头部吸干市场。

**T09 新品**(`new_products/engine.py`):
- `launch_success` = 连续 N 个月(可配)销量 ≥ 阈值(连续性按月份序数校验,断月重置);
- `current_survival` 与成功解耦:最新月仍在样本且达标 → True,**缺月 = unknown(None)而非死亡**,避免把缺数据误判为退场;
- 账号类型归一化(VC/SC)、地区、细分三个维度的成功率/成功占比汇总;同 ASIN 同月重复取最大销量(幂等)。

### 4.5 分类与补全(T04/T05)

- **T04 补全**:Provider 协议 + 主源/回退源链(SellerSprite 主、SIF/西柚备),`merge_profiles` 做字段级合并,`_is_complete_enough`(标题+品牌+卖点齐全)决定是否继续尝试回退;Semaphore 并发(默认 5)。
- **T05 分类**:Schema 由项目自定义维度和标签(`ClassificationLabel("nugget","咀嚼冰")`);Provider 协议对接任意 structured-output LLM(回调用 Callback 适配,核心包不绑厂商 SDK),另有规则关键词 Provider 用于测试/首 pass;**输出完整性保证**:请求的每个 ASIN 必有分类(漏了就填 unknown+low);人工可改(`apply_human_edits`,来源标记 human/confidence high);版本号 `cls-<内容哈希前12位>` 冻结,保证可追溯。

### 4.6 研究路由器(T10)

`router/service.py` 基于指标触发**规则化、可解释**的研究建议,每条建议必须带 `evidence_ids`:

| 触发条件 | 研究分支 | 优先级 |
|---|---|---|
| 最新月大盘 YoY < 0 | 大盘缩量原因研究(seasonality/渠道迁移/替代) | high |
| 细分逆大盘(大盘降、细分涨) | 细分迁移研究 | high |
| 头部虹吸信号 | 头部虹吸研究(品牌/广告深挖) | high |
| 小市场+高增长(阈值可配) | 小市场高速增长研究 | normal |
| 有新品样本 | 新品结构复核 | normal |
| 无任何触发 | manual_research(人工选择) | normal |

`confirm()` 强制人工多选确认后才产生 `confirmed_route_ids`;`confirmed_modules()` 把确认的路由展开成去重模块队列(TikTok、关键词季节性、广告等支线据此调度,不在第一版自由式 Multi-Agent 化)。

### 4.7 研究资料包(T11)

`dossier/generator.py` 单次运行产出 5 类文件:
- `data.json`:全量结构化 payload(含 `decision_owner: "human"`);
- `csv/`:13 张表(市场/细分/地区/分解/跨期/集中度/层级/分带/虹吸/新品结果与汇总);
- `report.md`:8 节人读报告(数据完整度→市场概览→细分结构→研究发现(带证据引用)→竞争→新品→路线→**人工判断空白表单**);
- `dashboard.html`:ECharts 双图(大盘月销量、细分月销量)+ 研究发现列表,单文件可离线打开;
- `manifest.json`:全部产物文件级 SHA256 清单(证据链完整性)。

### 4.8 Golden Case 回归基线(T00)

`fixtures/golden_cases/ice_maker/`:
- `archive_manifest.json`:客户制冰机案例 ZIP 的文件级 SHA256/大小清单(ZIP 本体不入库);
- `baseline_metrics.json`:冻结的历史期望输出,**仅用于回归比较**,不进生产计算;
- `scripts/build_golden_case_manifest.py` 可对原始 ZIP 重建并 `verify_archive_manifest` 逐文件校验。

## 五、安全与工程纪律

- 密钥只经 `${ENV_VAR}` 占位符从环境注入(`mcp/config.py:_expand_env`),配置文件零真实密钥;`tests/test_mcp_secrets_t02.py` 扫描防泄漏;
- SellerSprite 走本机已登录 Chromium/CDP 会话,Cookie 不上传;
- 依赖极简:核心只依赖 `mcp` SDK;langgraph/fastapi/polars 全部放 optional extras,离线测试可零 MCP 依赖运行;
- 全部计算幂等、确定性(排序键、去重规则、月份归一化都有测试锁定)。

## 六、当前边界与后续工作(来自 DEVELOPMENT_STATUS.md + 代码注释)

纯代码主干已完成;尚需真实环境联调的事项:

1. 用客户原始 ZIP 做文件级复核(T00 终验);
2. 真实 LangGraph interrupt/resume 联调(T01);
3. 登录 SellerSprite + 注入密钥做真实 `list_tools/call_tool`(T02);
4. 真实 SellerSprite 导出文件做字段映射验收(T03);
5. 接真实 structured-output LLM 并与历史制冰机分类抽查(T05/T07);
6. Golden Case 重算冻结指标、重算新品基线(895/195/21.8%/80%)(T06/T08/T09);
7. 前端实现研究支线多选确认 UI(T10);
8. 浏览器实际查看 ECharts、接真实来源索引(T11)。

后续插件化支线(TikTok、Google Trends、西柚关键词季节性、SP/SB/SBV 广告、Reddit VOC、专利深查)经 T10 人工确认后插入,不改核心数据契约。

## 七、快速上手(合并版)

```bash
cd product-research-workflow
uv venv --python 3.12 && uv pip install -e '.[dev,workflow,data]' -p .venv
.venv/bin/python -m pytest -q                      # 67 passed
.venv/bin/python scripts/run_offline_demo.py --output runtime/offline-demo
```

MCP 真实联调需:`npm install -g github:JieXi-11/SellerSprite` + 按 `docs/local/MCP_MAC_SETUP.md` 启动本机 Chromium/CDP。
