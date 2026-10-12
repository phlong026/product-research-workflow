# T06 专项评审｜Market Metrics Engine

## 范围

确定性计算，不调用 LLM：

- 月度市场总销量 / ASIN 数量
- 月度环比、同比
- 按产品分类：销量、ASIN 数、销量份额、ASIN 份额、同比/环比
- 按卖家地区：销量、ASIN 数、份额
- 市场规模变化 vs 产品份额变化的精确两因素分解
- Golden Case 计算结果比较工具

## 开源复用

- 参考现有 Amazon product-research / category report 中按品牌、卖家、市场份额聚合的结构。
- 制冰机案例要求的“总市场变化 + 细分份额变化”分解未发现可直接复用的成熟实现，因此使用标准恒等式独立实现。

## 关键公式

对于细分销量 `Q = Market × Share`：

`Q1 - Q0 = (M1-M0)×S0 + M1×(S1-S0)`

因此可精确分成：

- `market_contribution_sales`：市场规模变化贡献
- `share_contribution_sales`：份额变化贡献

两者之和必须严格重构细分销量变化。

## 边界

- 输入只能是 T03 清洗后的月度数据和 T05 已确认分类。
- 缺失上一月/上年同月时，MoM/YoY 返回 `None`，不伪造 0%。
- 分母为 0 时增长率返回 `None`。
- 负销量归零，避免脏数据污染聚合。
- 不输出 Yes/No、机会评分或业务建议。

## Golden Case

`compare_metrics` 只比较“代码重新计算的值”与冻结基线；不会把冻结结果写回生产计算，因此能用于后续制冰机全链路回归测试。
