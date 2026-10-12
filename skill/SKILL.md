---
name: crossborder-product-research
description: 跨境选品研究工作流。当用户要求研究亚马逊产品/类目市场(选品、市场分析、竞品分析、BSR 分析、新品分析、生成研究报告/资料包)时使用;由确定性引擎计算指标,在数据/分类/研究路线三个人工检查点等待用户确认。不用于一般编程或与选品研究无关的任务。
---

# 跨境选品研究工作流(Amazon Product Research)

驱动本 skill 背后的研究引擎完成:数据清洗 → 产品分类 → 市场指标 → 竞争/新品分析 → 研究路线 → 研究资料包(Dossier)。

## 红线(违反即失败)

1. **三个人工检查点必须呈现给用户并等待明确答复**。用户说"确认/同意/通过"才能执行 `resolve --approve`;用户修改意见转成 `--edit`/`--select` 参数。绝不能代替用户批准、跳过检查点或自动选择。
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

$PY $ENGINE/scripts/run_research.py classify --project <ID> \
    --label "nugget=咀嚼冰" --label "bullet=子弹冰" \
    [--rule "nugget=nugget, chewing ice"]
  → 分类检查点 classification_review(标签体系由用户定义;--rule 关键词规则可选)

$PY $ENGINE/scripts/run_research.py analyze --project <ID> \
    [--pair 2025-01:2026-01] [--period 2025-01:2025-04:2026-01:2026-04] \
    [--new-products-file <新品表>] [--sales-threshold 300 --consecutive-months 3]
  → 路线检查点 research_route_review

$PY $ENGINE/scripts/run_research.py dossier --project <ID> --output $ENGINE/runtime/<ID>/dossier
  → 生成 report.md / dashboard.html / data.json / csv/ / manifest.json
```

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
| classification_review | 各标签数量 + 从 `<项目目录>/classification.json` 读取每个 ASIN 的标签/置信度/理由 | "确认"→`--approve`;改分类→`--approve --edit ASIN=标签` |
| research_route_review | 每条建议的标题/理由/优先级(来自 analyze 输出) | 多选→`--approve --select id1,id2`(也允许不选任何支线直接 `--approve`) |

## 输入数据要求

- **月度 BSR 导出**(必需):CSV/XLSX/JSON,一个文件一个月,文件名含 `YYYY-MM`(否则单文件配 `--month`)。列名兼容中英文别名:ASIN、月销量/销量、品牌、商品标题、小类BSR/节点排名、卖家所属地、上架时间、父ASIN。典型来源:SellerSprite 商品调研导出。
- **新品跟踪表**(可选,供 T09):列 ASIN、月份、月销量、上架时间、账号类型(VC/SC)、卖家所属地、细分。
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
