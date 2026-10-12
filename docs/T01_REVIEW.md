# T01 专项评审｜Research Project + Workflow State + Human Checkpoint

## 范围
- Research Project 创建/查询。
- SQLite 持久化 Workflow State、步骤状态、人工 Checkpoint。
- 人工批准/驳回后恢复或保持暂停。
- FastAPI 最小接口。
- LangGraph `interrupt()` 适配器，延迟导入。

## 开源复用
- LangGraph 官方 `interrupt()` + checkpointer 模式：直接按官方模式接入，不自研图执行引擎。
- 持久业务状态不放进 LangGraph 内部，避免后续 PostgreSQL 迁移受限。

## 明确不做
- 不做最终 Yes/No。
- 不接 SellerSprite 真实数据（T02）。
- 不做市场指标（T06）。
- 不引入 Redis/PostgreSQL/Temporal。

## 评审结论
- 最小化：通过。
- Human-in-the-loop 边界：通过。
- 数据持久化可替换：通过。
- 外部依赖：LangGraph 仅运行时可选；离线核心测试无需安装。

## 测试结果
- `pytest`: 11 passed。
- FastAPI + SQLite 跨线程问题已在本任务内修复：连接启用 `check_same_thread=False`，仓储层用 `RLock` 串行化连接访问。
- 模块导入不再提前创建 `research_workflow.db`。
