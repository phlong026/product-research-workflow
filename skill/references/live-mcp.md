# 实时 MCP 数据源模式(按需阅读)

## 一键部署(setup_mcp.py)

```bash
$PY $ENGINE/scripts/setup_mcp.py --codex    # 部署 + 注册 MCP 到 Codex
$PY $ENGINE/scripts/setup_mcp.py --check    # 只体检
```

幂等步骤:venv 依赖 → `npm install -g github:JieXi-11/SellerSprite` → 从模板生成 `.env` 与 `config/mcp.json`(不覆盖已有)→ Codex 注册(sellersprite 走 `codex mcp add` stdio;sif/xiyou/apify 写入 `~/.codex/config.toml` 的 `url` + `http_headers` 段,首次修改自动备份)。**token 为空的服务器跳过注册**;在 `.env` 填入后重跑即注册。

## Token 配置(唯一需要人工的事)

编辑 `$ENGINE/.env`:

| 环境变量 | 数据源 | 说明 |
|---|---|---|
| `SIF_MCP_TOKEN` | SIF | 认证头默认 `secret-key`(`SIF_MCP_AUTH_HEADER` 可改) |
| `XIYOU_MCP_TOKEN` | 西柚 | Bearer 认证 |
| `APIFY_TOKEN` | Apify | Bearer 认证 |
| (无需 token) | SellerSprite | 本机 CDP Chrome 登录态,见下 |

同一份 `.env` 同时驱动:引擎自带网关(`ingest --live` 取数)和 Codex 注册(由 setup 脚本从 `.env` 读取并写入 config.toml;token 明文存于本机 Codex 配置,不入 Git)。

## SellerSprite 直连(月度数据主源)

1. 一次性登录:启动带 CDP 的 Chrome 并登录卖家精灵(详见 `$ENGINE/docs/local/MCP_MAC_SETUP.md`),或直接 `$PY $ENGINE/scripts/run_research.py login`;
2. 取数:`ingest --live --keyword ... --months 2025-01,2026-01`,走 `SellerSpriteConnector.product_research`(分页上限 100/页、总量约 2000,带重试),原始响应留档 `<项目目录>/raw/<月份>.json`。

## SIF / 西柚 / Apify 的用途

- 月度类目数据的**主源是 SellerSprite**;SIF 是产品/类目研究的回退源;
- 西柚工具面(`get_asin_bsr_trends`、`get_keyword_aba_trends`、`get_asin_traffic_trends` 等 18 个,见 `connectors/generic_mcp.py`)是**单 ASIN/关键词维度**,服务 T10 路由出去的支线研究(关键词季节性、流量深挖),不是月度全量源;
- Apify 是站外公开网页兜底;
- 注册进 Codex 后,助手也可在对话中直接调用西柚/SIF 工具做即席查询(不经引擎)。

## 当前状态与边界(对用户如实说明)

- T02 适配器与 `ingest --live` 已实现并有假网关测试,**但从未用真实账号联调**;首次真实使用时按错误提示排查(会话未登录→`login`;字段映射→`raw/` 留档 + `bsr_pipeline.ALIASES`);
- 真实联调门禁(沿用 docs/local/MCP_MAC_SETUP.md):`list_tools` 成功 + 至少一个只读查询 + 保存返回 schema 快照。
