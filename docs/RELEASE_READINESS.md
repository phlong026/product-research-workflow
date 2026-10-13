# 2026-10-12 上线评审与交接

## 可交付范围

当前适合**受控单机研究试点**：文件导入→清洗→人工数据复核→规则分类→人工分类复核→确定性市场/竞争/新品分析→人工确认研究路线→研究资料包。最终商业 Yes/No 仍由人填写。

不能按“无人值守、已接真实模型和供应商的多用户选品平台”上线。CLI 未执行 ASIN 补全；分类是关键词规则；市场解释是确定性模板。LLM callback 只是扩展口，未配置真实模型、费用限额或真实输出验收。确认研究路线只是登记待研究事项，没有自动执行 TikTok/广告/专利等支线。API 是实验性状态 CRUD，start/resume 不启动执行器。LangGraph 默认 InMemorySaver，进程内中断恢复通过离线测试，但没有跨进程持久执行器。

## 基线与缺陷修复批次

评审基线 main：`3b2f9746faddec33fc163c2b185e3f5a3f7b31ed`。本轮独立克隆后实跑原有 **75 tests passed**、compileall、offline demo；不是引用旧提交描述。首次 init 对缺失父目录的数据库路径实测失败。

| 批次 | 已复现问题 | 整改与证据 |
|---|---|---|
| 审批和状态 | 首启失败；数据未批可分类；旧检查点批准新文件；重导入/重分类复用旧指标；可选新品文件缺席未绑定；并发混代；审批数据库失败不可重试 | SQLite创建父目录；当前检查点校验；批准绑定工件 SHA256（含缺席）；下游失效；完整 CLI 命令事务串行；持锁恢复审批文件；严格布尔批准；`test_release_state.py` |
| 数据正确性 | 空/非法销量变0、Inf崩溃；非拉丁标题归一化变空误去重；负TopN/非法月份；缺月窗口虚构涨跌；未知存活算失败；补全错ASIN；无效证据部分通过 | 缺销量计异常并禁止 CLI 导入；Unicode标题；参数前置校验；缺月比较为null并列缺月；存活率只对已观测成功新品计算并披露覆盖率；ASIN匹配；无效引用整条丢弃；`test_release_metrics.py` |
| MCP接口 | 文本JSON未解包；嵌套result.data不识别；isError/HTTP业务错误当数据；login协程未await；无效月份可先请求；分页截断被当完整 | 解包与明确失败；登录单JSON；范围/站点/分页校验；有界重试+超时且不回显外部异常内容；本地真实SDK/stdio配合合成服务；`test_release_mcp.py` |
| 导出追溯 | 旧新品CSV混入新manifest；CSV公式注入；图表依赖CDN；源文件可被覆盖丢失 | 新目录staging发布，拒绝覆盖非空包；字符串公式转义；内嵌SVG离线图表；内容寻址输入快照、来源索引、审批记录与完整manifest；`test_release_export.py` |
| 安装/API | API匿名可读写；setup失败仍exit0；异常字串上调用.items；令牌落入config及备份 | API未配置令牌返回503，仅环回地址+Bearer访问；明确state-only；setup非零退出、错误收敛；Codex使用环境变量引用，新配置/备份0600；`test_release_deployment.py` |

历史测试中“非法销量=0”断言已改为排除非法记录并报告异常，这属于修正数据语义。品质报告其余缺字段计数针对成功解析的记录。新品表 `eligible=false/0/no/否` 会排除，缺失eligible沿用纳入；缺失新品销量报错。所有核心计算仍是 Python，无自动商业决策。

## MCP 契约证据与限制

本次读取的是公开上游 `JieXi-11/SellerSprite` 在 2026-10-12 可见的 main 源码，**未取得可确认的版本 SHA**：云环境直访 GitHub REST commit 元数据遭代理403，未绕过。以下链接是可变 main，后续真实验收必须由部署者固定供应商版本并保存脱敏 schema 快照。

- [server.mjs](https://github.com/JieXi-11/SellerSprite/blob/main/src/server.mjs)：稳定工具 `sellersprite_session_status` / `sellersprite_login` / `sellersprite_list_operations` / `sellersprite_describe_operation` / `sellersprite_call`；`jsonContent()` 使用 `content[].text`，错误带 `isError`；session `ready` 来自网页登录状态；login等待0–300秒。
- [adapter.mjs](https://github.com/JieXi-11/SellerSprite/blob/main/src/adapter.mjs)：业务响应封装 `status` 和 `result`，商品行位于 `result.data.items`。
- [operations.json](https://github.com/JieXi-11/SellerSprite/blob/main/data/operations.json)：`product_research` 的 marketplace、keyword、month、page、size 等语义及每页/总量限制。字段仍需真实账号验证，不能把商品调研Top销量样本自动解释为完整类目BSR普查。
- 本机安装 MCP SDK 2.3.0；`test_installed_sdk_stdio_transport_with_local_fixture` 实际启动本机合成stdio进程，验证SDK连接、工具分页和文本JSON协议，**不是供应商实连**。

SellerSprite 未登录、未启动 CDP；SIF/西柚/Apify 只有通用适配和配置，未核真实账户工具面。没有调用付费供应商、模型或采购服务，没有外部联系。未运行全局npm安装或Codex注册。`setup_mcp --check` 是静态体检，不是接口可用性证明。

请求预算：CLI最多60个月、每月20页、每页100条；单业务请求最多3次尝试，指数退避0.25/0.5秒，单次120秒超时。达到分页上限而无法确认完整时停止导入。最多约 `月份数 × 页数 × 3` 次商品请求，另有发现/会话检查；实际计费依供应商，代码没有货币成本账本。外部副作用任务（如Apify actor）不能通过自动重试承诺幂等。

## 安装与本地运行

先备份旧runtime，再检出整改分支。建议 Python 3.12 和 uv；不需要供应商密钥即可运行离线流程：

```bash
uv sync --frozen --all-extras --python 3.12
.venv/bin/python -m pytest -q -ra
.venv/bin/python -m compileall -q src scripts
.venv/bin/python scripts/run_offline_demo.py --output runtime/demo-new-version
.venv/bin/python scripts/run_research.py init --product '制冰机' --marketplace US
.venv/bin/python scripts/run_research.py projects
```

输出记录 `project_id`，按 [skill协议](../skill/SKILL.md) 执行 ingest、人工resolve、classify、人工resolve、analyze、人工resolve、dossier。使用**新的空输出目录**。不要对真实研究自动批准检查点；测试中的批准均为合成场景。

旧版本项目没有审批哈希，需从 ingest 开始重跑、逐个由人重新复核；不要手工修改approved标记。数据/分类/分析变更会使下游失效；驳回后status指导回到对应阶段。源文件快照使用项目相对路径，可随runtime迁移。每个数据库仅一条CLI命令持写事务；并发请求可能等待或报database locked，应等前一条结束再重试。进程强制终止或磁盘失败时可能留下未引用快照；哈希校验会阻止混代，重新执行失败阶段。不能用同一工件根目录搭配两个不同数据库并行操作。

可选本机实验性API（先用安全方式注入 `RESEARCH_API_TOKEN`，不在命令或聊天里打印真实值）：

```bash
RESEARCH_DB_PATH=runtime/api-experiment.db .venv/bin/python -m uvicorn research_workflow.api.app:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

请求携带 `Authorization: Bearer <本机令牌>`。它是单操作者共享令牌，不具备租户身份、项目级ACL或后台执行队列。不得反代对外开放；API实验数据库与CLI研究数据库分开。未配置令牌时全部路由含文档返回503，非环回请求返回403。

真实MCP待部署者在本机安全配置`.env`和浏览器登录；参见 [Mac说明](local/MCP_MAC_SETUP.md)。CLI网关按需读取`.env`；Codex配置只写令牌环境变量名称，Codex进程启动前需安全注入对应变量，它不会自动读取本仓库`.env`。旧版生成的config或备份可能仍有明文密钥，应在本机清理并按需要轮换，本轮未读取这些配置内容。

## 备份、恢复、回滚

1. 停止CLI和实验API写入，备份**数据库+整个projects目录+已导出的版本包**。不要只备份SQLite而漏掉工件。运行目录不入Git。
2. 例如默认路径可用 `tar -czf /安全备份目录/research-runtime-日期.tar.gz runtime/`，先确保备份目录权限仅操作者可访问；备份中不包含`.env`，密钥由Secret Manager或独立安全渠道管理。
3. 在新目录检出同一代码SHA，用uv.lock恢复依赖；解压runtime到新引擎根目录，用`projects`/`status`验证项目和待审检查点；用manifest逐文件验证资料包SHA256。
4. 可用SQLite `.backup` 或 Python `Connection.backup()` 做数据库一致性副本，但工件目录仍须在停写状态一起复制。不要复制正在写入的db文件后宣称可恢复。
5. 回滚代码使用旧版本的独立工作副本，同时恢复同一时点的runtime备份；本轮未引入破坏性表结构迁移，但新旧审批语义不同，不应让旧代码继续写新审批数据。现有导出包保持不可变；失败导出使用新目录重试。

## 剩余上线门槛

- 用业务授权的真实月度文件校验字段、站点、类目边界、父子体销量口径、TopN样本代表性和新品纳入规则。原始Golden Case ZIP未提供，冻结基线未改；本轮没有重算客户真实基线。
- 真实 MCP 只读list_tools/describe_operation/一次最小query后保存脱敏响应、固定版本和费用上限；新凭据由用户安全配置。当前未通过此门禁。
- 若需真实LLM，另行选择供应商、模型、预算、结构化输出与语义证据校验。现有守卫核引用ID存在与商业决策禁语，不能证明每个LLM数字或因果解释都正确；当前CLI不用该路径。
- 若需无人值守或多人上线，另行实现持久执行器、队列、任务级审批、审计身份和租户ACL。不能将本轮本地令牌等同于完整权限体系。
- Linux/Python3.12已验证；macOS浏览器与Windows部署、真实账号、原始客户数据未验证。没有自动部署或合并。

最终测试记录和独立复核见 [本轮证据](release-readiness-20261012/RESULTS.md)。
