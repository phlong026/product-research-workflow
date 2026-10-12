# T00 专项评审记录

## 目标

冻结“制冰机完整案例”为后续 Research Workflow 的 Golden Case，建立可重复回归基线，同时完成 MCP 依赖的安全占位配置。

## 评审结论

**通过，可进入 T01。**

### 已验证

- 客户源 ZIP 已生成 archive-level + file-level SHA256 manifest。
- 实际文件数：49（ZIP 目录项不计入）。
- 历史核心指标已独立固化到 `baseline_metrics.json`，并标注容差。
- `final_decision_owner=human` 被测试锁定，不允许把 Golden Case 误当自动 Yes/No 标签。
- SellerSprite / SIF / 西柚 / Apify 均只配置环境变量占位，无真实凭据。
- Python 语法编译通过。
- 离线测试：7/7 通过。

### 未在当前沙箱完成

当前容器无法访问 Python 包索引，因此无法在沙箱内安装 `mcp>=2` 做真实依赖导入 smoke test。MCP Gateway 的接口实现按 Model Context Protocol 官方 Python SDK v2 当前文档编写；在进入真实联调环境后，T02 必须增加一次 SDK 安装 + `list_tools()` 实连门禁。

## 开源复用决策

1. SellerSprite MCP：作为外部依赖直接使用，不复制其网页登录/扩展授权实现。
2. SIF MCP：直接连接现有远程 MCP；不重写其 27 个工具。
3. 西柚 MCP：直接连接现有远程 MCP；仅维护本项目 Adapter/Schema。
4. Apify：优先官方 MCP，不自行封装 Actor API。
5. LangGraph：T01 只复用 `interrupt()` + checkpoint 的 HITL 模式，不复制演示项目 UI。
6. `amazon-sorftime-research-MCP-skill`：只借鉴任务路由、报告结构和已有 Skill 拆分，不复用其自动选品结论逻辑。

## 安全评审

公开 GitHub 示例中发现过疑似真实密钥被直接写入 `.mcp.json`。本项目明确禁止：

- 把公开仓库中的 token/key 复制到任何环境；
- 把客户提供的密钥提交到 Git；
- 把 SellerSprite Cookie、extension token 或 session 文件写入代码仓库；
- 在日志中输出 Authorization/secret-key 等认证头。

## T01 进入条件

- T00 ZIP manifest 与 baseline 固定不再修改；若源资料变化，必须新建 Golden Case 版本，而不是覆盖 v1。
- T01 只实现 Research Project / Workflow State / Human Checkpoint，不开始市场计算。
