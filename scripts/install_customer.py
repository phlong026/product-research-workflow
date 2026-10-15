#!/usr/bin/env python3
"""客户安装包的安装后配置与体检(纯标准库 + 已装好的引擎;由 install.ps1 / Codex 调用)。

install.ps1 负责装 uv、建 .venv、同步依赖;本脚本负责其余全部且幂等:
  1. 生成 .env / config/mcp.json 骨架(只含占位符,不含任何密钥)
  2. 创建 runtime/ 目录
  3. 创建管理员账号(口令来自环境变量,或随机生成并写入仅本人可读的文件,不打印)
  4. 冒烟:真实启动 Web 工作台并确认接口有响应
  5. 输出 JSON 汇总(供 Codex/脚本读取)与人类可读提示

  python scripts/install_customer.py                      # 完整配置 + 冒烟
  python scripts/install_customer.py --check              # 只体检,不改任何东西
  python scripts/install_customer.py --admin-password-env XUANPIN_ADMIN_PASSWORD
"""
from __future__ import annotations

import argparse
import getpass
import importlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import setup_mcp  # noqa: E402  (同目录脚本,复用 .env/config 骨架生成)

DEFAULT_ROOT = HERE.parent
DEFAULT_PORT = 8800
PASSWORD_FILE = "initial_admin_password.txt"
REQUIRED_MODULES = ("mcp", "fastapi", "uvicorn", "openpyxl", "polars", "fastexcel", "langgraph", "anthropic")
# 数据源凭据:只检查「是否已填写」,绝不回显值。(卖家精灵 CDP 地址 .env 里自带默认值,不算凭据,见 sellersprite_local_prerequisites)
TOKEN_KEYS = {
    "SIF_MCP_TOKEN": "SIF", "XIYOU_MCP_TOKEN": "西柚洞察", "APIFY_TOKEN": "Apify",
    "ANTHROPIC_API_KEY": "LLM 分类(可选)",
}


def _say(text: str) -> None:
    print(text, flush=True)


def _engine_env(root: Path) -> None:
    os.environ["PRODUCT_RESEARCH_ENGINE"] = str(root)


def ensure_runtime(root: Path) -> list[str]:
    created = []
    for name in ("runtime", "runtime/projects"):
        path = root / name
        if not path.exists():
            path.mkdir(parents=True)
            created.append(name)
    return created


def _web_store(root: Path):
    _engine_env(root)
    from research_workflow.web.__main__ import _paths
    from research_workflow.web.store import WebStore

    return WebStore(_paths()[2])


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:  # 已存在则报错,不覆盖旧口令说明
        try:
            path.chmod(0o600)
        except OSError:
            pass
        handle.write(text)


def ensure_admin(root: Path, username: str, password_env: str | None, interactive: bool) -> dict[str, object]:
    """账号已存在则不动。口令来源:环境变量 > 交互输入 > 随机生成(写入私有文件,不打印)。"""
    from research_workflow.web import security

    store = _web_store(root)
    if store.get_user_by_name(username):
        return {"username": username, "status": "exists"}
    password = os.environ.get(password_env, "") if password_env else ""
    source = "environment"
    if not password and interactive and sys.stdin.isatty():
        password = getpass.getpass("设置管理员口令(至少 12 个字符,直接回车则随机生成): ")
        source = "prompt"
        if password and password != getpass.getpass("再次输入: "):
            raise ValueError("两次口令不一致")
    generated = False
    if not password:
        password, source, generated = secrets.token_urlsafe(18), "generated", True
    security.validate_password_strength(password)
    store.create_user(username, password, "admin")
    result: dict[str, object] = {"username": username, "status": "created", "password_source": source}
    if generated:
        target = root / "runtime" / PASSWORD_FILE
        _write_private(target, (
            f"管理员账号: {username}\n口令: {password}\n\n"
            "这是安装时随机生成的初始口令。请登录并记下后删除本文件;当前版本没有网页改口令功能,"
            "忘记口令时请用新用户名创建新的管理员账号(见 USER_GUIDE.md)。\n"))
        result["password_file"] = str(target)
    return result


def _free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise RuntimeError("找不到可用端口")


def smoke_test(root: Path, port: int, timeout: float = 30.0) -> dict[str, object]:
    """真实启动 Web 工作台,确认首页与 /api/me 有响应(未登录应为 401),然后关闭。"""
    port = _free_port(port)
    env = {**os.environ, "PRODUCT_RESEARCH_ENGINE": str(root), "PYTHONUTF8": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "research_workflow.web", "serve", "--port", str(port), "--insecure-cookies"],
        cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                err = (proc.stderr.read() or b"").decode("utf-8", "replace")[-600:] if proc.stderr else ""
                return {"ok": False, "detail": f"服务启动即退出: {err.strip()}"}
            try:
                try:
                    urllib.request.urlopen(f"{base}/api/me", timeout=2)
                    status = 200
                except urllib.error.HTTPError as exc:
                    status = exc.code
                home = urllib.request.urlopen(f"{base}/", timeout=2).status
                return {"ok": status == 401 and home == 200, "api_me_status": status, "home_status": home}
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.5)
        return {"ok": False, "detail": f"{timeout:.0f} 秒内服务没有响应"}
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _env_file_values(root: Path) -> dict[str, str]:
    return setup_mcp.parse_env_file(root / ".env")


def _chrome_path() -> str | None:
    candidates = [os.environ.get(k, "") + rest for k, rest in (
        ("ProgramFiles", r"\Google\Chrome\Application\chrome.exe"),
        ("ProgramFiles(x86)", r"\Google\Chrome\Application\chrome.exe"),
        ("LOCALAPPDATA", r"\Google\Chrome\Application\chrome.exe"))]
    candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    found = next((c for c in candidates if c and Path(c).exists()), None) or shutil.which("google-chrome") or shutil.which("chrome")
    return found


def run_check(root: Path) -> dict[str, object]:
    modules = {}
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(name)
            modules[name] = True
        except ImportError:
            modules[name] = False
    env_values = _env_file_values(root)
    tokens = {label: bool(os.environ.get(key) or env_values.get(key)) for key, label in TOKEN_KEYS.items()}
    web_db_exists = (root / "runtime" / "web.db").exists()
    try:
        admin_ready = bool(web_db_exists and _web_store(root).get_user_by_name("admin"))
    except Exception:  # noqa: BLE001 - 体检不应因库异常中断
        admin_ready = False
    return {
        "python": sys.version.split()[0], "python_ok": sys.version_info >= (3, 11),
        "modules": modules, "modules_ok": all(modules.values()),
        "files": {name: (root / name).exists() for name in (".env", "config/mcp.json", "runtime")},
        "admin_account": admin_ready,
        "data_source_credentials_filled": tokens,
        "sellersprite_local_prerequisites": {
            "node": bool(shutil.which("node")), "npm": bool(shutil.which("npm")),
            "sellersprite_mcp_installed": bool(shutil.which("sellersprite-mcp")), "chrome": bool(_chrome_path()),
        },
    }


def main(argv: list[str] | None = None) -> int:
    # 只在真实控制台上改编码(Windows 旧代码页会把中文打成乱码);被捕获/重定向的流(测试、管道)不动
    if sys.stdout is sys.__stdout__ and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="客户安装包:安装后配置与体检")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--check", action="store_true", help="只体检,不改任何东西")
    parser.add_argument("--admin-username", default="admin")
    parser.add_argument("--admin-password-env", help="从该环境变量读取管理员口令(不会出现在命令行里)")
    parser.add_argument("--non-interactive", action="store_true", help="不提示输入(Codex/脚本调用)")
    parser.add_argument("--no-smoke", action="store_true", help="跳过冒烟启动")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    _engine_env(root)

    report: dict[str, object] = {"root": str(root), "mode": "check" if args.check else "install"}
    if not args.check:
        report["env_files_created"] = setup_mcp.ensure_env_files(root)
        report["runtime_dirs_created"] = ensure_runtime(root)
        report["admin"] = ensure_admin(root, args.admin_username, args.admin_password_env, interactive=not args.non_interactive)
    report["check"] = run_check(root)
    if not args.check and not args.no_smoke:
        report["smoke"] = smoke_test(root, args.port)

    checks = report["check"]
    ok = bool(checks["python_ok"] and checks["modules_ok"]) and (args.check or args.no_smoke or bool(report["smoke"]["ok"]))
    report["ok"] = ok
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.check:
        admin = report["admin"]
        _say("")
        _say("安装完成。" if ok else "安装未通过,请看上面的 JSON(check / smoke 字段)。")
        if ok:
            _say("启动:双击 start.bat,浏览器打开 http://127.0.0.1:8800")
            if admin.get("password_file"):
                _say(f"管理员账号: {admin['username']};初始口令保存在 {admin['password_file']}(登录后请记下并删除该文件)")
            elif admin["status"] == "created":
                _say(f"管理员账号: {admin['username']};口令为你刚才设置的口令")
            _say("数据源凭据(SIF/西柚/Apify 等)请填入 .env 后再使用实时取数;不填也可用上传文件的方式研究。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
