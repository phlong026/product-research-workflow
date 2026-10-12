# T01 补全评审｜Workflow Run / Retry / Restart

本补丁对照开发任务书补齐此前 T01 只做到 Project 级状态的缺口。

## 新增

- `WorkflowRun` / `RunStatus`
- `workflow_runs`、`workflow_run_steps`、`workflow_resume_receipts`
- `run_id == LangGraph thread_id` 默认映射
- run 创建 / 查询 / step 状态 / pause / resume / retry
- `resume_key` 幂等收据，重复 resume 不重复处理命令
- SQLite 文件重开后 run 状态和数据引用仍可恢复
- API：
  - `POST /projects/{id}/runs`
  - `GET /runs/{id}`
  - `POST /runs/{id}/resume`
  - `POST /runs/{id}/retry`

## 简化取舍

任务书早期写 PostgreSQL；根据后续“前期尽可能简单”的决策，第一版继续 SQLite。Domain/Store 已隔离，未来迁移 PostgreSQL 不影响 T03–T11 业务代码。

## 仍需 Mac 真实依赖验证

LangGraph 本身的持久化 checkpointer 需要安装 workflow extra 后做 interrupt/resume 实连；业务 run/state 已独立持久化，不依赖内存 checkpointer 才能保存研究进度。
