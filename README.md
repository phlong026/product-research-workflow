# Product Research Workflow

Amazon/跨境选品的 **Research Workflow Engine**。系统自动完成数据获取、清洗、产品分类、市场/竞争/新品分析、研究路线建议和研究资料包；**最终商业 Yes/No 始终由人工决定**。

## 已完成

T00–T11 核心代码均已实现并通过独立测试/专项评审：

1. T00 — Ice Maker Golden Case / MCP foundation
2. T01 — Research Project / Workflow State / Human Checkpoint
3. T02 — SellerSprite / SIF / 西柚 / Apify connectors
4. T03 — Monthly BSR cleaning / dedupe / Top100
5. T04 — ASIN enrichment
6. T05 — Product classification + human review
7. T06 — Market metrics
8. T07 — Evidence-grounded market research analysis
9. T08 — Competition concentration / BSR layers
10. T09 — New-product success / survival / VC
11. T10 — Human-confirmed Research Router
12. T11 — Research Dossier

开发状态见 `DEVELOPMENT_STATUS.md`，专项评审见 `docs/T0X_REVIEW.md`，最终代码审计见 `docs/FINAL_CODE_AUDIT.md`。

## Skill 部署(AI 助手入口)

`skill/` 目录是把本引擎接入 zcode / Codex / Claude Code 的 skill 包装:

```bash
# 安装(符号链接,单一信息源)
ln -sfn "$(pwd)/skill" ~/.zcode/skills/crossborder-product-research
ln -sfn "$(pwd)/skill" ~/.codex/skills/crossborder-product-research
ln -sfn "$(pwd)/skill" ~/.agents/skills/crossborder-product-research
```

装好后在这些助手里说"研究一下制冰机美国市场"即可触发。**首次使用跑一键部署**(幂等,自动装 venv 依赖、SellerSprite MCP,生成 `.env` 骨架,并注册 MCP 进 Codex;之后只需在 `.env` 填 token 重跑一次):

```bash
.venv/bin/python scripts/setup_mcp.py --codex   # venv 不存在时先 uv venv + 安装依赖
.venv/bin/python scripts/setup_mcp.py --check   # 体检
```

skill 通过确定性 CLI 驱动引擎:

```text
scripts/run_research.py
  init → ingest(文件 或 --live MCP 直连) → [数据检查点] → classify → [分类检查点]
  → analyze → [路线检查点] → dossier
```

三个人工检查点在对话中呈现、由用户裁决(`resolve --approve/--reject`,支持 `--edit`/`--select`);全部子命令输出 JSON;`status`/`projects` 用于跨会话恢复。详见 `skill/SKILL.md`,实时 MCP 取数模式见 `skill/references/live-mcp.md`。

## Architecture

```text
MCP / Connector
   ↓
Normalized Data
   ↓
Deterministic Python calculations
   ↓
LLM classification / research explanation
   ↓
Human checkpoints
   ↓
Research Dossier
   ↓
Human business decision
```

- **LangGraph**：流程状态、暂停/恢复、人工检查。
- **Python**：所有可确定计算；LLM 不负责算数。
- **MCP**：SellerSprite/SIF/西柚/Apify；密钥只通过环境变量注入。
- **LLM**：分类、解释、聚类；不得自动给出最终商业 Yes/No。

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,workflow,data]'
cp .env.example .env
```

SellerSprite MCP：

```bash
npm install -g github:JieXi-11/SellerSprite
```

然后按 `docs/local/MCP_MAC_SETUP.md` 启动本机 Chromium/CDP 并登录 SellerSprite。

## Offline end-to-end demo

不需要任何密钥：

```bash
python scripts/run_offline_demo.py --output runtime/offline-demo
```

会生成：

```text
runtime/offline-demo/
├── report.md
├── dashboard.html
├── data.json
├── source_index.json
├── manifest.json
└── csv/
```

## Core LangGraph

`research_workflow.workflow.build_core_research_graph()` 实现固定主干：

```text
BSR数据 → 人工检查 → ASIN补全 → 分类 → 人工检查
→ 市场指标 → 市场解释 → 竞争 → 新品 → Research Router
→ 人工确认研究分支 → Dossier
```

支线研究（TikTok、关键词季节性、广告等）由 T10 按需调度，不把第一版做成自由式 Multi-Agent。

## Golden Case

`fixtures/golden_cases/ice_maker/` 保存制冰机案例文件 manifest 和冻结基线。冻结值仅用于**回归比较**，不会被写进生产计算。

## Security

- 绝不把真实 API key / token / Cookie 提交到 Git。
- 公开仓库里出现的 live-looking token 一律视为已泄露，禁止复用。
- SellerSprite 默认保留本机 Chrome 登录态，不上传 Cookie 到服务器。
