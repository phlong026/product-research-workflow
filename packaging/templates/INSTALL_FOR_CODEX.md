# 给 Codex 的安装说明

你(Codex)的任务:在用户的 Windows 电脑上安装本目录里的「跨境选品研究工作台」,并确认能启动。
本目录就是安装包解压后的根目录。**不要修改包内代码。**

## 规则(必须遵守)

1. 密钥只放在 `.env`,**不要**把任何 token/密钥/口令写进对话、命令行参数、日志或代码。需要用户提供密钥时,让用户自己编辑 `.env`(用记事本打开即可),你不要代填。
2. 管理员口令不要出现在命令行里:用环境变量传入,或让安装脚本自动随机生成。
3. 不要把工作台绑定到 0.0.0.0 或暴露公网;不要关闭任何人工检查点;不要自动批准检查点。
4. 安装失败时如实汇报脚本输出,不要伪造"安装成功"。

## 安装步骤

在本目录打开 PowerShell,依次执行:

```powershell
# 1) 安装(无人值守;会自动安装 uv、Python 3.12、锁定版本依赖,首次需联网)
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -NonInteractive

# 2) 体检(只读)
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Check
```

- 管理员口令:默认自动随机生成并保存在 `runtime\initial_admin_password.txt`(不会打印)。如果用户想自己指定,先让用户在**自己的** PowerShell 里设置环境变量(例如 `$env:XUANPIN_ADMIN_PASSWORD = "..."`),再加参数 `-AdminPasswordEnv XUANPIN_ADMIN_PASSWORD` 运行,口令至少 12 个字符。
- 直接调用 `install.ps1`(而不是 `install.bat`)可避免 `pause` 卡住;若用 `install.bat`,先设置 `XUANPIN_NO_PAUSE=1`。
- 脚本最后输出 JSON 汇总。成功的标志:`"ok": true`,且 `check.modules_ok` 与 `smoke.ok` 为 true。

## 常见问题

| 现象 | 处理 |
|---|---|
| `uv python install` / `uv sync` 失败 | 多半是网络或代理问题。让用户确认能访问 pypi.org 与 github.com(uv 会从那里下载 Python),设置好代理后重跑同一命令(可重复运行) |
| 无法安装 uv | 让用户手动安装 uv(https://docs.astral.sh/uv/ 或 `winget install astral-sh.uv`)后重跑 |
| 端口 8800 被占用 | 冒烟测试会自动换端口;正式启动前设置 `XUANPIN_PORT` 环境变量再运行 `start.bat` |
| 脚本被安全软件拦截 | 让用户按公司策略放行,不要绕过 |

## 安装后

1. 告诉用户:双击 `start.bat` 启动,浏览器访问 http://127.0.0.1:8800 ,账号 `admin`,初始口令在 `runtime\initial_admin_password.txt`(登录后记下并删除)。
2. 数据源凭据(可选,用于实时取数):请用户用记事本编辑 `.env`,填写 `SIF_MCP_TOKEN`、`XIYOU_MCP_TOKEN`、`APIFY_TOKEN` 等;卖家精灵走本机 Chrome 登录(见 `USER_GUIDE.md`)。不填也能用,改为上传导出的 CSV/Excel。
3. 运行 `check.bat` 可随时体检,它只显示「是否已填」,不显示密钥。
4. 如实告知用户本版本的边界:尚未用真实数据源联调(见 `README.md`)。
