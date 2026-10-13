# AGENTS.md — AI 助手开发与操作指南

本文件面向在 zcode / Codex / Claude Code 等 AI 助手中继续开发本项目的 agent 和人。
详细架构分析见 `docs/PROJECT_ANALYSIS.md`,任务级状态见 `DEVELOPMENT_STATUS.md`。

## 项目一句话

Amazon 跨境选品「研究工作流引擎」:数据获取 → 清洗 → 分类 → 市场指标 → 竞争/新品分析 → 研究路线建议 → 研究资料包;**最终商业 Yes/No 永远由人工决定**。

## 红线(任何改动不得违反)

1. **系统不得自动产出商业 Yes/No / 进入退出建议**。`tests/test_scope_guardrail.py` 和 `analysis/market.py:_assert_no_business_decision()` 锁死该边界;新增任何 LLM 输出路径都必须过这道守卫。
2. **LLM 不做算数**。所有指标由确定性 Python 计算(`metrics/`、`competition/`、`new_products/`);LLM 只做分类(`classification/`)和解释(`analysis/`),解释必须引用预计算证据 ID,引用不实的发现整条丢弃。
3. **密钥只走环境变量**。`config/mcp.example.json` 只含 `${ENV_VAR}` 占位符;绝不提交真实 token/Cookie;`tests/test_mcp_secrets_t02.py` 会扫描。
4. **Golden Case 冻结值不进生产计算**。`fixtures/golden_cases/ice_maker/baseline_metrics.json` 仅用于回归比对,不得改动或用于运行时逻辑。
5. 人工检查点(`data_review` / `classification_review` / `route_review`)不可绕过或自动批复。

## 环境与常用命令

本机系统 Python 是 3.9,项目要求 ≥3.11,必须用 uv 或 Homebrew Python:

```bash
# 一次性搭建
uv venv --python 3.12
uv pip install -e '.[dev,workflow,data]' -p .venv

# 测试(改完代码必须跑,全部通过才算完成)
.venv/bin/python -m pytest -q

# 静态语法检查
.venv/bin/python -m compileall -q src scripts

# 离线端到端 demo(不需要任何密钥;改了主流程必须跑)
.venv/bin/python scripts/run_offline_demo.py --output runtime/offline-demo

# 实验性本机状态API：先安全注入 RESEARCH_API_TOKEN；不启动研究执行器
RESEARCH_DB_PATH=runtime/api-experiment.db .venv/bin/python -m uvicorn research_workflow.api.app:app --host 127.0.0.1 --port 8000 --no-proxy-headers

# Agent CLI(skill 的确定性执行器;改了 skill 协议或命令后必须跑)
.venv/bin/python -m pytest tests/test_cli_research.py tests/test_cli_live.py -q

# MCP 一键部署 / 体检(改 setup_mcp.py 后必须跑其测试)
.venv/bin/python scripts/setup_mcp.py --check
.venv/bin/python -m pytest tests/test_setup_mcp.py -q
```

真实 MCP 数据源联调(SellerSprite/SIF/西柚/Apify)见 `docs/local/MCP_MAC_SETUP.md`;未配置密钥时所有离线测试照常可跑。

## 架构地图(src/research_workflow/)

| 目录 | 任务 | 职责 |
|---|---|---|
| `domain/` | T01 | Project/Run/Step/Checkpoint 数据模型与状态枚举 |
| `store/sqlite.py` | T01 | SQLite 持久化(参数化 SQL) |
| `workflow/` | T01 | `WorkflowService` 生命周期;`full_graph.py` 14 阶段 LangGraph 主图(3 处 `interrupt()` 人工检查点) |
| `api/app.py` | T01 | FastAPI REST |
| `mcp/` | T00 | MCP 注册表/网关,`${ENV}` 展开,认证头构建 |
| `golden_case/` | T00 | ZIP 文件级 SHA256 manifest 与基线校验 |
| `connectors/` | T02 | `base.py`(ToolGateway 协议+重试)、`sellersprite.py`(业务适配:分页/信封解包)、`catalog.py`(能力路由表) |
| `data/` | T03 | `bsr_pipeline.py` 清洗(别名映射/两级去重/TopN/质量报告)、`io.py`(CSV/JSON/XLSX) |
| `enrichment/` | T04 | ASIN 补全:主源+回退源合并、并发 5 |
| `classification/` | T05 | Schema+Provider 协议+人工修改+内容哈希版本冻结 |
| `metrics/` | T06 | 市场指标:MoM/YoY/份额/精确两因子分解/跨期比较 |
| `analysis/` | T07 | 证据约束的 LLM 市场解释+决策边界守卫 |
| `competition/` | T08 | 集中度/BSR 累积层级/头腰尾带/虹吸信号 |
| `new_products/` | T09 | launch success(连续达标)与 current survival 解耦,缺月=未知 |
| `router/` | T10 | 规则化研究分支建议,人工 confirm 后生效 |
| `dossier/` | T11 | JSON+CSV+Markdown+离线SVG HTML+SHA256 manifest |

## 代码约定

- **模块模式**:每个分析模块 = `models.py`(dataclass)+ 一个实现文件(`service.py`/`engine.py`/`metrics.py`/`generator.py`)+ `__init__.py` 导出公共 API;测试放 `tests/test_<主题>_t<任务号>.py`。
- **类型**:dataclass(slots)+ `Protocol`,不引入 pydantic 到核心计算层(API 层除外);新依赖必须进 optional extras,核心包只依赖 `mcp` SDK。
- **确定性**:所有排序/去重/归一化必须有稳定的 tie-breaker(参考 `data/bsr_pipeline.py:_prefer`);同输入必须同输出(测试依赖此性质)。
- **月份格式**:统一 `YYYY-MM`,经 `_normalize_month` 类函数校验。
- **外部调用**:必须走 `connectors/base.py:with_retries`,信封解析不得泄漏到业务层。
- **TDD**:新功能先写失败测试;每个任务的测试文件独立(`test_*_tXX.py`),集成测试在 `test_full_workflow_integration.py`。
- 提交信息用中文;一个逻辑变更一个 commit。

## 给 agent 的操作提示

- skill 部署:`skill/` 是 zcode/Codex 的 skill 包装(入口 SKILL.md,执行器 `scripts/run_research.py`);改 CLI 输出格式或检查点协议时,同步更新 `skill/SKILL.md` 并保持 `tests/test_cli_research.py` 通过。
- 注意引擎内部 step_key 不完全一致:路由检查点在 `ResearchRouter` 中叫 `research_route_review`,在 LangGraph 主图中叫 `route_review`;CLI 已兼容两者,新增代码引用时留意。

- 改任何计算逻辑后:跑 `pytest -q` + 离线 demo,两者都绿才算完成。
- 判断改动影响面:`grep -r "evidence"` 类 ID 格式(`market:2026-01`、`decomp:基期:现期:细分`)是 T07/T10/Dossier 的公共契约,改指标字段名会破坏引用链。
- `runtime/` 是运行产物目录,不要提交;`research_workflow.db`、`*.db-journal` 已在 `.gitignore`。
- 尚未完成的真实环境联调清单见 `DEVELOPMENT_STATUS.md` 表格右列,不要声称"已联调"。
