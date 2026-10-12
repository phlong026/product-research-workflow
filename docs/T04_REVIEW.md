# T04 专项评审｜ASIN Enrichment

## 范围

- 唯一 ASIN 批量补全：标题、卖点、品牌、父 ASIN、上架时间、卖家地区、主图、价格、评分、评论数。
- SellerSprite 作为主数据源，SIF / 西柚 / 其他 MCP 可作为兜底。
- 所有供应商响应先转换成 `ASINProfile`，下游不依赖供应商字段。

## 开源复用

- SellerSprite MCP / Sorftime MCP 已有 ASIN 详情、变体等查询能力，不重新实现 Amazon 页面抓取。
- 复用“数据源适配层”思路，不复制第三方项目中的自动选品决策逻辑。

## 设计取舍

1. 第一版不建 ASIN 缓存表：T03 已先抽唯一 ASIN，足以避免同一项目重复请求。
2. 主源有值时不被兜底源覆盖；兜底只补缺失字段。
3. SellerSprite operation 名称可配置。真实 operation 名称在 Mac 连接 SellerSprite MCP 后通过 catalog 确认，不硬耦合内部 API。
4. 单 ASIN 请求失败不终止整个批次，最终通过 `missing_profiles` 暴露缺失量，交人工检查。

## 门禁

- 去重 ASIN 后才调用数据源。
- 供应商字段不能进入核心模型。
- 不包含 Yes/No 或评分决策。
- 单元测试覆盖：常见响应 envelope、字段别名、主/兜底合并、失败降级。

## 本地联调待办（Mac）

- SellerSprite MCP 实连后确认 ASIN detail operation 名称与参数格式。
- 用 3–5 个制冰机 ASIN 验证标题/卖点/卖家地区映射。
