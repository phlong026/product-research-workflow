# Mac 本地 MCP 联调清单

这些步骤只在真实联调阶段执行，不阻塞纯代码开发。

## SellerSprite
1. 安装 Node.js 22+。
2. `npm install -g github:JieXi-11/SellerSprite`
3. 启动专用 Chrome：
   ```bash
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
     --remote-debugging-port=9222 \
     --user-data-dir="$HOME/Library/Application Support/SellerSpriteMCP/Chrome"
   ```
4. 在该 Chrome 登录卖家精灵网页和卖家精灵扩展。
5. `SELLERSPRITE_CDP_URL=http://127.0.0.1:9222`
6. 真实门禁：`session_status -> list_operations -> describe_operation -> call`。

## SIF / 西柚 / Apify
只把真实 Token 写入本机 `.env` 或 Secret Manager；不要提交 Git。

## 真实联调门禁
- MCP `list_tools()` 成功。
- 至少执行一个只读查询。
- 保存返回 schema 快照供 Adapter 测试。
