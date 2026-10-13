# Web 研究工作台(T4-1 / T4-2 / T4-3)

研究员用浏览器完成:建项目 → 上传/取数 → 三个人工检查点 → 分析 → 下载资料包。多人使用,项目彼此隔离。

## 启动

```bash
uv pip install -e '.[dev,workflow,data,xlsx]' -p .venv
.venv/bin/python -m research_workflow.web create-user --username alice --role admin   # 口令交互输入(≥12 字符)
.venv/bin/python -m research_workflow.web serve --port 8800 --insecure-cookies        # 本机 http 调试
```

- 默认只监听 127.0.0.1;对外提供服务必须放在 **HTTPS 反向代理**之后(Cookie 带 `Secure`)。
- 数据位置:`runtime/research.db`(引擎)、`runtime/projects/`(项目工件)、`runtime/web.db`(账号/会话/作业)。可用 `RESEARCH_DB_PATH` / `RESEARCH_PROJECTS_ROOT` / `RESEARCH_WEB_DB_PATH` 覆盖。
- 作业在**子进程**中调用引擎 CLI(`python -m research_workflow.cli`),因此引擎的人工检查点、哈希绑定、审批失效规则与命令行完全一致,Web 不会绕过它们。

## 安全模型(上线前须经人工安全评审)

已由独立评审代理审过一轮(1 HIGH / 6 MEDIUM / 10 LOW,无 CRITICAL),HIGH 与全部 MEDIUM 已修复并有回归测试(`tests/test_web_security.py`)。**这只是自动化评审,不能替代人工安全评审。**

- 口令 PBKDF2-HMAC-SHA256(60 万次迭代,随机盐);库中只存会话令牌的 SHA-256 摘要。用户名统一规范化(NFKC+去空白+casefold),限流/查库/建号共用。
- 会话 Cookie `HttpOnly; SameSite=Strict; Secure`;所有写请求需 `X-CSRF-Token`(登录/`GET /api/me` 返回)。
- 登录限流三层:用户名+来源 IP 连续失败 5 次、单 IP 跨用户名 30 次、单用户名跨来源 60 次,各锁 60 秒;失败表有容量上限;口令校验并发闸(4)防 CPU 被打满。
- 项目访问:owner / editor / viewer;无权访问一律 404;admin 可访问全部(项目 ID 必须是 UUID 且在引擎库中存在,杜绝路径穿越)。
- 用户输入只会成为子进程 argv 的单个元素(`--flag=value`),不经 shell;参数全部经 pydantic 校验并有数量上限;工件下载为白名单;请求体上限 1 MB(上传除外)。
- 子进程只继承白名单环境变量;作业错误只回显引擎主动给出的业务错误,traceback/stderr 只进服务端日志;回传结果去掉服务器绝对路径。
- 上传仅 csv/xlsx/json,流式落盘并硬限 50 MB/文件(含 chunked)、每项目 200 个/1 GB;文件名清洗,按内容哈希落盘。
- 每用户同时进行的作业 ≤3;每项目同时只有一个作业(DB 唯一索引兜底)。
- 审计日志:登录成功/失败/限流、建号、授权、作业提交写入 `research_workflow.web.audit` logger(尚未落库)。
- **仍未做(backlog)**:会话空闲超时与"注销全部会话";撤销成员/转移所有权接口;审计事件落库;LLM/实时取数的按用户配额;`__Host-` Cookie 前缀;多进程/多实例部署(限流与项目锁为进程内,须换共享存储;当前只支持单进程);改口令/找回界面(管理员用 CLI 重建账号);HTTPS 终止(交给反向代理)。

## API(JSON;除登录外均需会话 Cookie,写请求需 CSRF 头)

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` `{username,password}` | 设置会话 Cookie,返回 `{username,role,csrf_token}`;失败 401,锁定 429 |
| POST | `/api/auth/logout` | 注销 |
| GET | `/api/me` | `{username,role,csrf_token}` |
| POST | `/api/admin/users` `{username,password(≥12),role}` | 仅 admin |
| POST | `/api/projects` `{product,marketplace(2 字母),primary_keyword?}` | 创建,返回 `{project_id}`(201) |
| GET | `/api/projects` | 我可见的项目 `[ {project_id,product,marketplace,status,role,created_at} ]` |
| GET | `/api/projects/{id}` | 引擎现场:`state/steps`、`pending_checkpoint{checkpoint_id,step_key,prompt,payload}`、`artifacts[]`、`next`(ingest/classify/analyze/dossier/resolve/done)、`role` |
| POST | `/api/projects/{id}/members` `{username,role: editor\|viewer}` | owner 分享 |
| GET | `/api/projects/{id}/members` | 成员名单 `[ {username,role} ]`(任何成员可看) |
| DELETE | `/api/projects/{id}/members/{username}` | owner 移除成员(不能移除所有者) |
| GET | `/api/projects/{id}/exports/table{1,2,3}.csv` | 下载 `export-table` 作业导出的表 |
| PUT | `/api/projects/{id}/uploads?filename=x.csv` | 请求体=文件原始字节;返回 `{upload_id,filename,sha256,size}`(editor+) |
| POST | `/api/projects/{id}/jobs` `{command,params}` | 提交作业,202 `{job_id}`;项目已有进行中作业 409;参数非法 422 |
| GET | `/api/projects/{id}/jobs` | 作业列表(新→旧) |
| GET | `/api/jobs/{job_id}` | `{status: queued\|running\|succeeded\|failed, result, error, attempts, ...}` |
| POST | `/api/jobs/{job_id}/retry` | 仅 failed 可重试,202 新 `job_id` |
| GET | `/api/projects/{id}/artifacts/{name}` | 白名单 JSON 工件(如 `classification.json`、`quality.json`、`coverage.json`、`nodes.json`、`route_plan.json`) |
| GET | `/api/projects/{id}/dossiers` | 已生成资料包版本 |
| GET | `/api/projects/{id}/dossiers/{vN}/download.zip` | 下载资料包(含 report.md、research_workbook.xlsx、manifest.json 等) |

### 作业命令与参数

| command | params |
|---|---|
| `discover-nodes` | `{keyword?, month?}` → 节点候选,**主节点必须人工确认** |
| `ingest` | 文件模式 `{uploads:[upload_id...], month?, top_n?}`;取数模式 `{live:true, keyword, months:"auto"或"2025-01,2026-01", nodes?:[nodeIdPath...], candidate_n?, top_n?}` → 进入 `data_review` 检查点 |
| `classify` | `{labels:{key:名称}, rules?:{key:"词1,词2"}, dimension?, provider?: rules\|llm, enrich?: bool}` → `classification_review` 检查点;`needs_review` 行必须在 resolve 时用 `edits` 指定 |
| `analyze` | `{new_products_upload?, sales_threshold?, consecutive_months?, early_window?, promotion_threshold?, retention_threshold?, study_month?, pairs?, periods?}` → `research_route_review` 检查点 |
| `resolve` | `{checkpoint_id, approve: bool, edits?:{ASIN:标签}, select?:[route_id...]}` —— **只能由用户明确点击触发,前端不得自动批准** |
| `export-table` | `{table: 1\|2\|3}` → 生成 `exports/table{n}.csv`,经 exports 接口下载 |
| `import-table` | `{table, upload: upload_id}` → 校验人工修改后的表;成功后旧审批失效并重新请求对应检查点(表1→data_review,表3→classification_review,表2→需重跑 classify);校验失败不改任何状态 |
| `import-evidence` | `{kind: trends\|report-excerpts\|tiktok-units\|patents\|patent-search-log\|ads\|big-brand\|offsite\|cross-category, upload: upload_id, meta?: {key: value}}` → 登记外部证据(证据登记不是人工检查点,但文件哈希进入资料包 manifest);各 kind 必填的 meta 见 `skill/SKILL.md` |
| `build-market-forecast` | `{horizon_years, scope?: top_sample, method?: cagr\|linear, granularity?: monthly\|annual, target_scope?, coverage_ratio?, coverage_source?, low_growth?, high_growth?}` → 预测(跨口径外推必须带有来源的覆盖率) |
| `build-cross-channel` | `{geo?: global\|us, segment?: total\|countertop}` → Amazon+TikTok 事实整合(不含任何 Yes/No) |
| `dossier` | `{}` → 服务端选择新版本目录 |

## 前端要求(红线)

1. 三个人工检查点(`data_review` / `classification_review` / `research_route_review`)必须展示给用户并等待其明确点击;不得自动 `approve`。
2. 不显示、不生成任何商业 Yes/No 结论;资料包的人工决策栏由用户自己填写。
3. 缺月、待人工分类、补齐冲突、口径假设必须原样、醒目地展示(不得隐藏警告)。
4. CSP 为 `default-src 'self'`:只能用独立的 `/static/*.js`、`/static/*.css`,不能内联脚本,不能引用 CDN。
