# Product Research Workflow

Amazon/跨境选品的 **Research Workflow Engine**。当前可运行受控单机文件研究流程（清洗、规则分类、确定性市场/竞争/新品分析、人工确认研究路线和资料包）；**最终商业 Yes/No 始终由人工决定**。

## 当前交付边界

2026-10-12 整改说明、真实接口验收缺口、部署与备份步骤见 [RELEASE_READINESS](docs/RELEASE_READINESS.md)。CLI分类采用关键词规则，市场解释采用模板，ASIN补全尚未接入CLI；真实MCP、LLM与客户Golden Case未验收。实验API仅维护状态，不能后台执行研究；默认关闭，启用后仅环回地址加令牌可访问。

**差距整改(分支 `feat/phase1-data-chain`，尚未合并)**：在上述基础上新增节点发现与多节点采集、近三年月份窗口、三值跨父体去重、地区/卖点补齐、可选 LLM 分类(歧义标待人工)、三张表人工校正协议、新品留存率原文口径与新品队列、单点名次矩阵与卖家口径集中度、外部证据登记(Trends/TikTok/专利/广告/大品牌/站外，不联网)、综合 Excel 工作簿，以及**多人 Web 研究工作台**(`python -m research_workflow.web serve`，见 [Web 工作台](docs/WEB_WORKBENCH.md))。逐项进度见 [DEVELOPMENT_STATUS](DEVELOPMENT_STATUS.md)，待业务拍板的口径见 [口径确认单](docs/口径确认单.md)。以上均用合成数据离线验证，**未经真实供应商联调与业务样本对账**；Web 鉴权代码上线前须经人工安全评审。

## 模块实现

以下模块具有实现与离线测试，模块存在不代表所有外部能力已接通：

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
- **LLM 扩展口**：支持接入分类、解释回调，当前CLI使用规则/模板；不得自动给出最终商业 Yes/No。

## Install

```bash
uv sync --frozen --all-extras --python 3.12
source .venv/bin/activate
test -f .env || cp .env.example .env
chmod 600 .env
```

SellerSprite MCP：

```bash
npm install -g github:JieXi-11/SellerSprite
```

然后按 `docs/local/MCP_MAC_SETUP.md` 启动本机 Chromium/CDP 并登录 SellerSprite。

## Offline end-to-end demo

不需要任何密钥，输出目录必须为空或不存在（重复运行请换新目录）：

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
