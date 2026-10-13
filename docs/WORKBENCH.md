# 多人研究工作台（Web）

浏览器里完成：建项目 → 上传/修改文件 → 提交各阶段任务 → 查看进度 → 人工检查点确认 → 下载成品。
**研究逻辑仍全部在 `scripts/run_research.py`**；工作台只负责身份、权限、上传、任务队列和展示，
不绕过任何人工检查点、审批哈希绑定或决策边界。

## 启动

```bash
# 1. 安装（含 web 依赖）
uv pip install -e '.[dev,workflow,data,llm]' -p .venv

# 2. 在服务器上创建第一个管理员（令牌只显示一次，库里只存哈希）
WORKBENCH_DB=runtime/workbench.db .venv/bin/python scripts/workbench_admin.py create-user --name alice --role admin

# 3. 启动服务（引擎库与项目目录和 CLI 共用同一套路径）
export WORKBENCH_DB=runtime/workbench.db RESEARCH_DB_PATH=runtime/research.db RESEARCH_ROOT=runtime/projects
.venv/bin/python -m uvicorn research_workflow.web.app:create_app_from_env --factory --host 127.0.0.1 --port 8000
```

对外提供服务必须放在带 TLS 的反向代理（nginx/Caddy）之后；服务本身不处理 HTTPS。
管理员登录网页后可在“用户管理”创建成员并拿到令牌；也可用 `workbench_admin.py rotate-token/disable-user`。

## 权限模型

| 角色 | 能做什么 |
|---|---|
| 管理员（全局） | 管理用户；访问所有项目（等同项目负责人） |
| 项目 owner | 管理成员、查看审计日志、全部编辑动作 |
| 项目 editor | 上传文件、提交任务、裁决人工检查点 |
| 项目 viewer | 只读：状态、任务、下载 |
| 非成员 | 看不到项目（返回 404，不暴露是否存在） |

- 令牌 256 位随机数，库里只存 SHA-256 哈希；停用用户立即失效。
- 所有上传、任务提交、检查点裁决都写入审计日志（谁、何时、什么参数）。**裁决人 = 当前登录用户**。

## 任务执行

- 研究动作是 **命令白名单 + 结构化参数**（`web/commands.py`），不接受任意命令/路径；文件参数只接受本项目已上传文件的 ID。
- 任务持久化在 SQLite 队列，单个后台线程串行执行（用子进程调用 CLI，沿用引擎的数据库事务与失败回滚）。
- 服务重启时仍处于 running 的任务会被标记为失败（可能已有部分副作用，**不自动重跑**），由人确认状态后重新提交。
- `live` 供应商取数（卖家精灵等）默认关闭，需设置 `WORKBENCH_ALLOW_LIVE=1`。注意：卖家精灵登录态是服务器本机的一个浏览器会话，**所有用户共享同一个供应商账号**。
- 上传大小默认上限 50MB，类型限 csv/xlsx/xlsm/json/pdf/txt/png/jpg。

## 已知限制（不要当作已解决）

- 单 worker 串行：长任务会让同一服务内其他项目的任务排队。
- 没有登录失败限流、没有 SSO/双因素；令牌泄露等同账号泄露（网页把令牌放在浏览器 sessionStorage，关闭标签页即清除）。
- 没有任务取消按钮、没有实时日志流（页面每 2.5 秒刷新状态）。
- 数据库为 SQLite，适合小团队；多机部署需迁移到 PostgreSQL 与外部队列。
- 供应商会话共享、真实接口字段、真实模型费用尚未验收（见 `DEVELOPMENT_STATUS.md`）。
- 备份需同时备份 `workbench.db`、`research.db` 和整个 `projects/` 目录。
