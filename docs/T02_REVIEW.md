# T02 专项评审｜MCP / Connector Layer

## 开源复用决策
- SellerSprite：直接依赖 `JieXi-11/SellerSprite` MCP，不复制网页登录/Cookie/扩展签名逻辑。
- SIF：远程 MCP，小适配层。
- 西柚：远程 MCP；保留 17 个已知工具名用于能力验证，不复制第三方业务判断逻辑。
- Apify：官方 `https://mcp.apify.com` Streamable HTTP MCP。
- `amazon-sorftime-research-MCP-skill`：MIT，可借鉴其 MCP 场景拆分、data/report/dashboard 三层结构；本项目不复用其中自动选品决策评分。

## 关键修复
- T00 的 MCP HTTP 客户端改为官方 Python SDK v2 当前模式：`Client` + `httpx2.AsyncClient` + `streamable_http_client`。
- 支持分页 `list_tools()`。
- 业务层只依赖 `ToolGateway` Protocol，方便测试和后续替换 MCP SDK。

## 安全
- 仓库无真实 Key。
- 公开仓库中出现过明文 Key；全部视为泄露凭据，不使用。
- SellerSprite 默认本机 CDP，不保存 Cookie/Token。

## 不做
- 不在服务器重写 SellerSprite 抓取。
- 不在 T02 做业务数据清洗。
- 不绑定任何厂商返回字段到核心数据库。
