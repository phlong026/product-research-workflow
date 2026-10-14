---
name: crossborder-product-research
description: 跨境选品研究工作流。当用户要求研究亚马逊产品/类目市场(选品、市场分析、竞品分析、BSR 分析、新品分析、生成研究报告/资料包)时使用;由确定性引擎计算指标,在数据/(补齐)/分类/研究路线人工检查点等待用户确认。不用于一般编程或与选品研究无关的任务。
---

# 跨境选品研究工作流(Amazon Product Research)

驱动本 skill 背后的研究引擎完成:数据清洗 → 产品分类 → 市场指标 → 竞争/新品分析 → 研究路线 → 研究资料包(Dossier)。

## 红线(违反即失败)

1. **人工检查点(数据、补齐[做了补齐才有]、分类、研究路线)必须呈现给用户并等待明确答复**。用户说"确认/同意/通过"才能执行 `resolve --approve`;用户修改意见转成 `--edit`/`--select` 参数。绝不能代替用户批准、跳过检查点或自动选择。
2. **不产出商业 Yes/No**。汇报只陈述数字与证据,禁止出现"建议进入/不建议进入/最终决策"类结论;报告末尾的人工判断栏保持空白,由用户自己填写。
3. **不编造数据**。所有数字必须来自引擎输出或用户提供的文件;引擎未算出的内容要明说"未计算/缺数据"。

## 引擎定位与首次部署

引擎根目录 `$ENGINE` 的解析顺序:
1. 环境变量 `PRODUCT_RESEARCH_ENGINE`;
2. 本 skill 目录(解析符号链接后)的上一级——skill 位于 `<引擎>/skill/`;
3. 都找不到时向用户询问引擎路径。

**首次使用**:用户把项目文件夹丢进 skill/Codex 后,先跑一键部署(幂等,重复执行安全):

```bash
"$ENGINE/.venv/bin/python" "$ENGINE/scripts/setup_mcp.py" --codex
# venv 不存在时先: cd "$ENGINE" && uv venv --python 3.12 && uv pip install -e '.[dev,workflow,data]' -p .venv
```

该脚本完成:venv 依赖 → SellerSprite MCP(npm 全局安装)→ `.env`/`config/mcp.json` 骨架 → MCP 注册进 Codex(sellersprite stdio + 带认证头的 sif/xiyou/apify)。**token 未填的服务器会被跳过并提示**——指导用户在 `$ENGINE/.env` 填入 `SIF_MCP_TOKEN`/`XIYOU_MCP_TOKEN`/`APIFY_TOKEN` 后重跑同一命令即完成注册。体检用 `setup_mcp.py --check`。之后所有命令用 `PY="$ENGINE/.venv/bin/python"` 执行,引擎状态默认落在 `$ENGINE/runtime/`。

**SellerSprite 直连前还需登录**(一次性,本机操作):
```bash
"$PY" "$ENGINE/scripts/run_research.py" login          # 等待在 CDP Chrome 中登录卖家精灵
```
未登录时 `ingest --live` 会返回会话不可用错误,按提示处理。

## 运行边界

分类默认使用规则；`classify --provider llm`（需 `ANTHROPIC_API_KEY` 与可选依赖 `.[llm]`，输出稳定性未做生产验收）会把歧义项标为待人工，待人工行必须用 `--edit` 逐一指定后才能批准；`enrich` 经 SellerSprite 补齐缺失地区与卖点（需已登录，冲突写入 `enrichment.json`），补齐后必须在**补齐检查点 `enrichment_review`** 人工确认才能分类/分析/出资料包（不做补齐则无此关口；`classify --enrich` 已废弃）；市场解释仍为确定性模板；确认研究路线只登记待办，不自动执行支线。上线边界见 `docs/RELEASE_READINESS.md`。重跑上游会使下游审批和工件失效；旧检查点不能审批新结果，缺销量/空月份数据须先修正。导出必须选择新的空目录。旧版本项目须从ingest重新人工复核。Codex注册仅保存令牌环境变量引用；启动Codex前须安全注入变量，不能假设它自动读取引擎`.env`。

## 执行协议

子命令全部输出 JSON。流程与命令:

```
$PY $ENGINE/scripts/run_research.py init --product "制冰机" --marketplace US
  → 记下 project_id

$PY $ENGINE/scripts/run_research.py ingest --project <ID> --file <月度导出文件> [--file ...]
  → 数据检查点 data_review

# 或 MCP 直连取数(需已完成部署+登录,详见 references/live-mcp.md)
$PY $ENGINE/scripts/run_research.py ingest --project <ID> --live \
    --keyword "ice maker" --months 2025-01,2026-01 [--max-pages 20]
  → 原始响应留档 <项目目录>/raw/,同样进入数据检查点
  --months auto = 近三年可得月份窗口(缺月写入 coverage.json 并在输出 missing_months 中显式列出,不当 0);
  显式月份若无数据会直接报错;每月先取 --candidate-n(默认 200)候选池,清洗后截取 --top-n(默认 100)。

# 节点发现(取数前,主节点必须人工确认)
$PY $ENGINE/scripts/run_research.py discover-nodes --project <ID> [--keyword "ice maker"] [--month 2026-01]
  → 输出节点候选与 ASIN 数、主节点建议(needs_human_confirmation=true),写 nodes.json
$PY $ENGINE/scripts/run_research.py ingest --project <ID> --live --keyword ... --months ... --nodes <nodeIdPath1,nodeIdPath2>
  → 按 节点×月 分存原始响应(raw/<月>__<节点>.json),多节点样本按 ASIN 合并去重

# 商品补齐(可选;表2 的卖家所属地与卖点;需 SellerSprite 已登录)
$PY $ENGINE/scripts/run_research.py enrich --project <ID>
  → 补齐检查点 enrichment_review:展示补齐汇总、冲突、失败;确认前不能 classify;
    驳回或被作废后 next=enrich;重新 enrich 会作废旧确认与下游分类

$PY $ENGINE/scripts/run_research.py classify --project <ID> \
    --label "nugget=咀嚼冰" --label "bullet=子弹冰" \
    [--rule "nugget=nugget, chewing ice"] [--provider rules|llm]
  → 分类检查点 classification_review(标签体系由用户定义;--rule 关键词规则可选)

$PY $ENGINE/scripts/run_research.py analyze --project <ID> \
    [--pair 2025-01:2026-01] [--period 2025-01:2025-04:2026-01:2026-04] \
    [--new-products-file <新品表>] [--sales-threshold 300 --consecutive-months 3] \
    [--promotion-threshold N --retention-threshold N --study-month YYYY-MM] [--skip-queue-filter]
  → 推起阈值（人工腰部销量）与留存阈值**没有内置默认值**，须业务给出（`--promotion-threshold` / `--retention-threshold`，或显式的旧版 `--sales-threshold` 同时作为二者回退）；两者缺任一项则不计算新品指标，并在 analysis_context 与资料包“缺失证据”里写明原因；提供 --study-month 时新品输入最大月份必须一致；
    同时产出 new_product_queue.json(新品队列、老带新排除清单)与 iteration.json(产品迭代进程)
  → 路线检查点 research_route_review

$PY $ENGINE/scripts/run_research.py dossier --project <ID> --output $ENGINE/runtime/<ID>/dossier
  → 生成 report.md / dashboard.html / data.json / csv/ / manifest.json
```

外部证据登记与预测(**证据登记不是人工检查点**:不触发 resolve、不参与 data_review / classification_review 哈希,但会进入 dossier 的 `evidence/` 与 manifest):

```bash
# 登记外部证据(可在 ingest 之后任意时刻、多次执行;同 kind 追加为带序号的条目,不覆盖历史)
$PY $ENGINE/scripts/run_research.py import-evidence --project <ID> \
    --kind trends|report-excerpts|tiktok-units|patents|patent-search-log|ads|big-brand|offsite|cross-category \
    --file <文件> [--meta key=value ...]
  → 原文件按 SHA256 快照到 raw/,结构化结果存 external/<kind>.json,登记 external/index.json
  → 校验失败(缺来源、含 GMV 字段、专利无检索日志…)原样输出库报错,不改任何状态
  → 必填 --meta:trends = geo timeframe source_name source_url as_of;tiktok-units = source_name source_url as_of
    (可选 low_growth high_growth);big-brand = brand product_type source_name retrieved_at(可选 aba_rank threshold url as_of);
    cross-category = source_name retrieved_at(可选 url as_of brand);其余 kind 的来源列在文件内
  → patent-search-log 要在 patents 之后导入才能把检索命中挂到专利记录上

# analyze 之后的可选预测(预测值=外推,假设随结果登记;库拒绝缺覆盖率的跨口径外推)
$PY $ENGINE/scripts/run_research.py build-market-forecast --project <ID> --horizon-years N \
    [--method cagr|linear] [--granularity monthly|annual] [--low-growth g --high-growth g] \
    [--target-scope amazon_channel|full_market --coverage-ratio R --coverage-source "来源说明"]
  → market_metrics 月度销量只覆盖 Top 样本(--scope 只能是 top_sample);要报告更大口径必须带有来源的覆盖率
  → 与 tiktok-units 对齐成同期间对比需要 --granularity annual(仅聚合 12 个月齐全的年份)
$PY $ENGINE/scripts/run_research.py build-cross-channel --project <ID> [--geo us --segment countertop]
  → forecast.json 与 tiktok-units 都存在时生成 Amazon×TikTok 事实整合(人工决策栏留空)
```
汇报时:带预测标签的数字必须说明是「预测·假设见下」;专利要区分「未检索」与「已检索-范围内未发现」并附「不构成法律意见」;来源不可访问/缺失的证据照报告原样转述,不补数。重新 analyze 或追加 tiktok-units 后需重跑 build-market-forecast / build-cross-channel,否则 dossier 会因证据过期而拒绝导出;篡改 external/ 下文件同样会被拒绝。

三张表的人工校正(表1=清洗月度数据,表2=唯一 ASIN 的地区/卖点,表3=分类结果):

```bash
$PY $ENGINE/scripts/run_research.py export-table --project <ID> --table 1|2|3 --output <文件.csv>
# 用户在表格软件中修改(不得增删列;表1 不能新增行,销量缺失不得当 0;表3 标签须在体系内;卖点用 || 分隔)
$PY $ENGINE/scripts/run_research.py import-table --project <ID> --table 1|2|3 --file <修改后文件>
```
校验失败不改任何状态;成功后旧审批立即失效:表1 → 重新请求 data_review;表3 → 重新请求 classification_review;表2 → 重新请求 enrichment_review(保留机器补齐,人工修改优先),确认后再 classify。只有重新确认的版本才会进入下游。

验收对账(只读;参考表列 key,expected[,tolerance_abs,tolerance_pct],key 形如 market.2026-01.total_sales / point_rank.2026-01.3.sales / new_products.summary.retention_rate_pct):

```bash
$PY $ENGINE/scripts/run_research.py reconcile --project <ID> --reference <参考表> [--output <差异报告.csv>]
```
全部 MATCH 才算通过;MISMATCH/MISSING(未知)须逐项查明原因,不得为凑数改容差,缺失不会被当作 0。

裁决与恢复:

```bash
# 裁决检查点(--approve 与 --reject 二选一;分类可带 --edit,路线可带 --select)
$PY $ENGINE/scripts/run_research.py resolve --project <ID> --checkpoint <CP> --approve \
    [--edit B1=bullet ...] [--select route_id1,route_id2]

# 查看项目现场(待裁决检查点、已完成步骤、下一步)与列出全部项目
$PY $ENGINE/scripts/run_research.py status --project <ID>
$PY $ENGINE/scripts/run_research.py projects
```

## 检查点的对话呈现

| 检查点 | 向用户展示 | 用户答复 → 动作 |
|---|---|---|
| data_review | 每月 输入/输出行数、两级去重删除数、质量警告数 | "确认"→`--approve`;提出问题→`--reject`,按用户修正后重新 ingest |
| enrichment_review | 补齐汇总(补了多少地区/卖点)、冲突与失败清单、是否含人工修改的表2(来自 enrich/import-table 输出与 status 的检查点载荷) | "确认"→`--approve`;要改→`export-table --table 2` 修改后 `import-table` 回传(会重新请求本检查点);驳回→`--reject` 后重新 `enrich` |
| classification_review | 各标签数量 + 从 `<项目目录>/classification.json` 读取每个 ASIN 的标签/置信度/理由 | "确认"→`--approve`;改分类→`--approve --edit ASIN=标签` |
| research_route_review | 每条建议的标题/理由/优先级(来自 analyze 输出) | 多选→`--approve --select id1,id2`(也允许不选任何支线直接 `--approve`) |

## 输入数据要求

- **月度 BSR 导出**(必需):CSV/XLSX/JSON,一个文件一个月,文件名含 `YYYY-MM`(否则单文件配 `--month`)。列名兼容中英文别名:ASIN、月销量/销量、品牌、商品标题、小类BSR/节点排名、卖家所属地、上架时间、父ASIN。典型来源:SellerSprite 商品调研导出。
- **新品跟踪表**(可选,供 T09):列 ASIN、月份、月销量、上架时间、账号类型(VC/SC)、卖家所属地、细分、父ASIN(用于识别老带新)。
- 用户没有数据文件时:优先询问是否走 MCP 直连(需已部署且 SellerSprite 已登录);两者都不满足时说明所需文件;如用户只想看效果,可运行 `$PY $ENGINE/scripts/run_offline_demo.py --output $ENGINE/runtime/offline-demo` 演示。

## 汇报规则(dossier 生成后)

1. 读 `report.md`,按节摘要**事实**:数据完整度、市场概览(最新月销量/同比/环比)、细分与地区结构、研究发现(附证据 ID)、竞争集中度与头腰尾、新品成功率、已确认的研究路线。
2. 给出 `dashboard.html`(浏览器打开)和 dossier 目录的绝对路径。
3. 提醒用户:最终商业判断在报告第 8 节,由人工填写。

## 故障处理

- 命令失败输出 `{"error": ...}` 且退出码非 0:把 error 原文告诉用户,不要臆测原因。
- 状态不明时先 `status`;用户提到"上次/继续之前的研究"时用 `projects` 找到项目再 `status`。
- `ingest --live` 报"所有月份解析出 0 行"时:读 `<项目目录>/raw/<月份>.json` 的实际字段名,指导用户或自行把销量/排名列名补进 `src/research_workflow/data/bsr_pipeline.py` 的 ALIASES(这是唯一允许动引擎代码的场景,改后跑 `pytest -q`)。
- MCP 会话/登录类错误先读 [references/live-mcp.md](references/live-mcp.md);SIF/西柚/Apify 的 token 配置也在其中。
