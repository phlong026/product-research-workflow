# 2026-10-12 验证证据

评审基线：`3b2f9746faddec33fc163c2b185e3f5a3f7b31ed`。整改分支：`fix/release-readiness-20261012`。下列结果均为本轮云环境实际运行；测试数据为仓库样例或合成输入，不代表真实供应商、模型或客户数据验收。

## 门禁结果

| 检查 | 结果 | 证据 |
|---|---|---|
| 原始版本全量测试 | 75 passed，0.66s | [基线日志](baseline-pytest.log) |
| 修复后全量测试 | 125 passed，1.74s，无跳过 | [最终日志](final-pytest.log) |
| `python -m compileall -q src scripts` | exit 0 | 本轮执行；无输出 |
| `git diff --check` | exit 0 | 本轮执行；无输出 |
| 离线端到端 demo | exit 0；17个文件大小及SHA256全部验证 | [哈希验证](manifest-check.log) |
| 全新虚拟环境锁定安装 | `uv sync --frozen --all-extras --python 3.12` 成功 | [安装日志](clean-install.log) |
| 全新环境首次 CLI init | 创建缺失的数据库父目录并返回项目ID | [首启日志](clean-init.log) |
| 依赖一致性 | `uv pip check`：65个包兼容 | [依赖检查](dependencies-check.log) |
| 构建 sdist + wheel | 成功 | [构建日志](build.log) |
| MCP 静态配置体检 | Python依赖可用，供应商安装/令牌缺失符合预期 | [体检结果](setup-check.log) |
| 停写备份跨目录恢复 | 删除原DB、原项目及输入CSV后，恢复副本仍可status及导出 | `test_stopped_runtime_backup_restores_on_another_path` |
| CI 定义 | 新增离线测试、编译和demo工作流 | `.github/workflows/offline-checks.yml`；此记录不宣称远程CI已执行 |

环境：Linux、Python 3.12.14、MCP SDK 2.3.0、LangGraph 1.2.14、FastAPI 0.143.0、uv 0.12.23；见 [环境记录](environment.json)。uv缓存仅放临时可写目录，没有修改全局配置。

## 失败回归与独立复核

先复现、后修复的回归覆盖审批绕过和并发、缺失数据/指标语义、MCP协议/分页、导出与配置。最初失败数分别为状态7、指标14、MCP5、嵌套MCP1、导出3、部署4、严格审批4、复核补充4、快照2。它们是各修复阶段的失败记录，不能相加作为最终测试数量；原始日志留在本地 `runtime/release-readiness/*-red.log`。

非作者只读审查由独立审查代理完成，逐项审阅差异、运行测试和专项复现，未修改实现。独立审查补出的可选文件缺席绑定、审批失败回滚、命令并发、分页ASIN重叠、品牌大小写拆分、自定义Codex配置目标和破损快照复用均已修复。它另核验了64个已安装的第三方依赖版本与uv.lock一致、离线demo的17个文件哈希。

最终独立复核：**125 passed in 1.87s**，`git diff --check` 通过；新增恢复测试与独立临时恢复场景都验证成功。未发现新增阻塞问题。结论是可交付受控单机研究试点，真实供应商、模型及多人生产部署仍未验收。

冻结 Golden Case 文件未修改；没有用其冻结值代替运行时计算。`test_installed_sdk_stdio_transport_with_local_fixture` 使用已安装的真实SDK和本地合成stdio服务，其他供应商测试使用stub/mock；两者都不是供应商实连。

## 未执行与原因

- 真实SellerSprite/SIF/西柚/Apify及模型调用：用户明确禁止本轮付费调用，未配置真实凭据；未启动浏览器登录。
- 上游SellerSprite精确版本SHA：公开源码已读取，但GitHub REST commit元数据请求被代理403拒绝，未绕过；可变源码链接与契约限制见[评审说明](../RELEASE_READINESS.md)。
- 客户原始Golden Case ZIP、真实业务数据验收：未提供输入，不以合成回归代替。
- macOS/Windows与浏览器视觉验收：本机为Linux，未具备相应环境；HTML生成和离线资产由自动测试覆盖。
- 实际部署、主分支合并、真实MCP注册：未执行，不在本轮授权范围。

部署、业务边界、费用限制和恢复步骤见[上线评审与交接](../RELEASE_READINESS.md)。选定的日志可公开提交；未提交运行数据库、密钥、真实业务资料或虚拟环境。
