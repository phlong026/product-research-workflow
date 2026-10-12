# T10 专项评审｜Research Router

## 定位

Router 只回答：**“下一步还应该研究什么？”**

它绝不回答：**“这个产品应该 YES 还是 NO？”**

## 开源复用

- 复用 LangGraph 条件路由 + Human-in-the-loop 的设计范式。
- 不采用自由规划 Multi-Agent；路由分支全部显式注册、可测试、可人工确认。

## 当前内置研究触发

1. `market_decline_deep_dive`
   - 客观条件：最新市场 YoY < 0
   - 建议研究：季节性、渠道迁移、产品替代
2. `segment_migration:<segment>`
   - 客观条件：大盘同比 < 0 且该细分同比 > 0
   - 建议研究：细分、新品、站外渠道
3. `small_market_fast_growth`
   - **不会默认触发**；只有项目显式配置“小市场”和“高速增长”阈值后才启用
4. `head_suction:<month>`
   - 客观条件：TopN 增长而更宽 BSR 层级下降
5. `new_product_structure_review`
   - 已有新品分析时，提示继续检查品牌 / VC-SC 结构

## Human Gate

- Router 只能生成 suggestion。
- 必须由人工确认 `confirmed_route_ids` 后，`confirmed_modules()` 才能生成执行队列。
- 可人工选择 0 个分支。
- 不允许人工注入系统未注册的 route ID。

## 评审结论

- ✅ Research Decision 与 Business Decision 分离。
- ✅ 未经客户定义的“小/快”等阈值不擅自硬编码。
- ✅ 所有支线显式、可测试、可审计。
- ✅ 后续 TikTok / Google Trends / 关键词季节性 / 广告等模块可作为 `next_modules` 插件接入。
