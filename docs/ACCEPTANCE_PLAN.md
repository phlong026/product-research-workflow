# 真实数据验收方案（第五批）

本环境没有真实卖家精灵账号、客户原始数据和人工对账结果，因此**以下验收尚未执行**。
代码侧已提供对账工具 `reconcile`；验收必须由持有真实数据的人按本方案执行并留存证据。

## 0. 先由客户确认的口径（工程暂定，见 `METRIC_DEFINITIONS.md`）

1. 跨父体去重：评分数为 0 或缺失的商品不判重——是否接受？
2. 留存率分子：是否要求“历史上曾连续达标”？（当前不要求）
3. 推广阈值数值、窗口月数（默认 3，含上架月）。
4. ~~逐名次销量的“名次”~~ **已确认：按位次取第 N 名**（先按节点排名排序再取第 N 个，见 `METRIC_DEFINITIONS.md`）。
5. 表1/2/3 的含义（当前：清洗 Top / 商品补齐 / 分类）是否与客户一致。
6. 卖家集中度需要“卖家名”字段：真实接口里字段叫什么？（别名表 `data/bsr_pipeline.py: ALIASES["seller"]`）

## 1. 真实接口联调（卖家精灵）

- `setup_mcp.py --check` 全绿；`login` 成功；`ingest --live` 取一个已知小类一个月数据。
- 对照 `raw/<月份>.json` 的真实字段名，补齐 `ALIASES`（销量、排名、评分、评分数、卖家、父 ASIN、上架时间）和
  `data/nodes.py` 的节点字段别名；`nodes-discover --live` 的节点提议与人工判断一致。
- 记录：分页是否完整（总数 = 抓取数）、每月 TopN 是否覆盖节点边界。

## 2. 端到端案例

| 案例 | 目的 |
|---|---|
| 制冰机（客户已有人工结果） | 逐项对账；黄金样例 `fixtures/golden_cases/ice_maker` 只作回归，不进入生产计算 |
| 另一种市场类型（如季节性强/小而快增长） | 验证口径与路由不是为制冰机“调”出来的 |

每个案例：多节点 `ingest` → `table-export` 人工检查 → `enrich` → `classify`（rules 与 llm 各一次对比）→ `analyze` → `dossier`。

## 3. 对账

1. 人工结果整理成参考表 `key,expected[,tolerance_abs,tolerance_pct]`（key 见 `reconcile.py` 说明）。
2. `run_research.py reconcile --project <ID> --reference ref.csv --output rep.csv`（工作台里也可提交 `reconcile` 任务）。
3. 全部 MATCH 才算通过；MISMATCH/MISSING 逐项归因：口径差异（回到第 0 节）、数据缺失、还是缺陷。**不得为凑数改容差。**

## 4. 分类质量与费用（LLM）

- 抽查 ≥100 个商品，与人工分类对比，记录一致率、unknown 比例、低置信度占比。
- 读取 `classification.json → meta.llm.usage`（请求数、token、失败批次、拒绝数）核算单项目费用，设定月度预算上限。
- 如果拒绝/失败批次 >0，确认这些商品都落为 unknown 并被人工处理。

## 5. 部署与恢复

- Windows / Mac 各装一遍（`pyproject` 锁定依赖），跑 `pytest -q` 与离线 demo。
- 工作台：多用户权限、上传限制、任务中断后的行为按 `WORKBENCH.md` 逐条验证。
- 备份恢复演练：`workbench.db + research.db + projects/` 整体恢复到另一路径后项目可继续。

## 6. 外部证据

- 首期要求覆盖的槽位见 `evidence-status`；逐槽位录入来源并由人复核。TikTok 销量必须是台数。
- 样本占美国市场比例只在有年度台数证据且样本覆盖 12 个月时才会计算。
