#!/usr/bin/env python3
"""一键部署:环境 + MCP 安装 + Codex 注册。

设计目标:把整个项目文件夹交给 skill/Codex 后,跑一次本脚本即可完成
venv、SellerSprite MCP(npm)、.env/config 骨架,并(可选 --codex)把
MCP 服务器注册进 Codex;之后只需在 .env 里填 token 再重跑一次。

  python scripts/setup_mcp.py            # 环境准备(venv/npm/.env 骨架)
  python scripts/setup_mcp.py --codex    # 同时注册 MCP 到 ~/.codex/config.toml
  python scripts/setup_mcp.py --check    # 只体检,不改任何东西

幂等:重复运行安全;已存在的配置不覆盖,token 变更后重跑会刷新注册段。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

HTTP_SERVERS = (
    # (注册名, URL 键, 认证头键, scheme 键, token 键)
    ("sif", "SIF_MCP_URL", "SIF_MCP_AUTH_HEADER", "SIF_MCP_AUTH_SCHEME", "SIF_MCP_TOKEN"),
    ("xiyou", "XIYOU_MCP_URL", "XIYOU_MCP_AUTH_HEADER", "XIYOU_MCP_AUTH_SCHEME", "XIYOU_MCP_TOKEN"),
    ("apify", "APIFY_MCP_URL", "APIFY_MCP_AUTH_HEADER", "APIFY_MCP_AUTH_SCHEME", "APIFY_TOKEN"),
)


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip().strip('"').strip("'")
        if key.strip() and value:
            env[key.strip()] = value
    return env


def _toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # JSON 字符串转义与 TOML 基本字符串兼容


def build_http_headers(header: str, scheme: str, token: str) -> dict[str, str]:
    value = f"{scheme} {token}".strip() if scheme else token
    return {header or "Authorization": value}


def toml_section_http(name: str, url: str, headers: dict[str, str]) -> str:
    lines = [f"[mcp_servers.{name}]", f"url = {_toml_quote(url)}"]
    if headers:
        lines.append("")
        lines.append(f"[mcp_servers.{name}.http_headers]")
        lines.extend(f"{key} = {_toml_quote(value)}" for key, value in headers.items())
    return "\n".join(lines) + "\n"


def replace_toml_section(text: str, name: str, section_text: str) -> str:
    """幂等替换 [mcp_servers.<name>] 段(连同其子表),保留文件其余内容。"""
    pattern = re.compile(
        rf"(?ms)^\[mcp_servers\.{re.escape(name)}\]\n.*?(?=^\[(?!mcp_servers\.{re.escape(name)}\.)|\Z)"
    )
    if pattern.search(text):
        return pattern.sub(lambda _: section_text.rstrip("\n") + "\n\n", text, count=1).rstrip("\n") + "\n"
    return text.rstrip("\n") + "\n\n" + section_text.rstrip("\n") + "\n"


def ensure_env_files(engine: Path) -> dict[str, bool]:
    created = {}
    for template, target in (
        (engine / ".env.example", engine / ".env"),
        (engine / "config" / "mcp.example.json", engine / "config" / "mcp.json"),
    ):
        if not target.exists() and template.exists():
            target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
            created[str(target.relative_to(engine))] = True
        else:
            created[str(target.relative_to(engine))] = False
    return created


def registered_names(config_path: Path) -> set[str]:
    if not config_path.exists():
        return set()
    text = config_path.read_text(encoding="utf-8")
    return set(re.findall(r"(?m)^\[mcp_servers\.([^\].]+)\]", text))


def register_codex(engine: Path, config_path: Path) -> dict[str, str]:
    """注册 4 个 MCP 到 Codex;sellersprite 走官方 CLI,HTTP 三个走 TOML 段替换。"""
    result: dict[str, str] = {}
    env = parse_env_file(engine / ".env")
    existing = registered_names(config_path)

    if shutil.which("codex") is None:
        result["sellersprite"] = "fail: 未找到 codex CLI"
    else:
        probe = subprocess.run(
            ["codex", "mcp", "get", "sellersprite"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if probe.returncode == 0:
            result["sellersprite"] = "skip: 已注册(codex mcp get sellersprite)"
        else:
            cdp = env.get("SELLERSPRITE_CDP_URL", "http://127.0.0.1:9222")
            added = subprocess.run(
                [
                    "codex", "mcp", "add", "sellersprite",
                    "--env", f"SELLERSPRITE_CDP_URL={cdp}",
                    "--", "sellersprite-mcp",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            result["sellersprite"] = (
                "ok: 已注册(stdio)" if added.returncode == 0
                else f"fail: {added.stderr.strip()[:200] or added.stdout.strip()[:200]}"
            )

    for name, url_key, header_key, scheme_key, token_key in HTTP_SERVERS:
        url = env.get(url_key, "")
        token = env.get(token_key, "")
        if not token:
            result[name] = f"skip: .env 中 {token_key} 为空;填好后重跑本脚本即注册"
            continue
        if not url:
            result[name] = f"skip: .env 中 {url_key} 为空"
            continue
        headers = build_http_headers(
            env.get(header_key, "Authorization"), env.get(scheme_key, ""), token
        )
        if config_path.exists():
            backup = config_path.with_suffix(".toml.bak-xuanpin-setup")
            if not backup.exists():
                backup.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
        section = toml_section_http(name, url, headers)
        text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        action = "刷新" if name in existing else "注册"
        config_path.write_text(replace_toml_section(text, name, section), encoding="utf-8")
        result[name] = f"ok: 已{action}(http, 认证头 {next(iter(headers))})"
    return result


def run_step(label: str, fn) -> tuple[str, str]:
    try:
        return label, fn()
    except Exception as exc:  # 每步独立失败,不中断整体
        return label, f"fail: {exc}"


def ensure_venv(engine: Path) -> str:
    py = engine / ".venv" / "bin" / "python"
    if py.exists():
        check = subprocess.run(
            [str(py), "-c", "import research_workflow"],
            capture_output=True, text=True, timeout=60, cwd=engine,
        )
        if check.returncode == 0:
            return "ok: .venv 已就绪"
        return "fail: .venv 存在但无法 import research_workflow;删除 .venv 后重跑"
    uv = shutil.which("uv")
    if uv:
        subprocess.run([uv, "venv", "--python", "3.12"], cwd=engine, check=True, timeout=300)
        subprocess.run(
            [uv, "pip", "install", "-q", "-e", ".[dev,workflow,data]", "-p", ".venv"],
            cwd=engine, check=True, timeout=600,
        )
        return "ok: 已用 uv 创建 .venv 并安装依赖"
    return "fail: 未找到 uv;请先安装 uv 或手动 python3.12 -m venv .venv && pip install -e '.[dev,workflow,data]'"


def ensure_npm_mcp() -> str:
    if shutil.which("sellersprite-mcp"):
        return "ok: sellersprite-mcp 已在 PATH"
    npm = shutil.which("npm")
    if not npm:
        return "fail: 未找到 npm;请先安装 Node.js 22+"
    installed = subprocess.run(
        [npm, "install", "-g", "github:JieXi-11/SellerSprite"],
        capture_output=True, text=True, timeout=480,
    )
    if installed.returncode == 0 and shutil.which("sellersprite-mcp"):
        return "ok: 已安装 sellersprite-mcp"
    return f"fail: npm 安装失败: {(installed.stderr or installed.stdout).strip()[:300]}"


def check_status(engine: Path, config_path: Path) -> dict[str, object]:
    env = parse_env_file(engine / ".env")
    tokens = {
        key: ("已填" if env.get(key) else "空")
        for _, _, _, _, key in HTTP_SERVERS
    }
    return {
        "venv": (engine / ".venv" / "bin" / "python").exists(),
        "sellersprite_mcp": bool(shutil.which("sellersprite-mcp")),
        "env_file": (engine / ".env").exists(),
        "mcp_config": (engine / "config" / "mcp.json").exists(),
        "tokens": tokens,
        "codex_cli": bool(shutil.which("codex")),
        "codex_registered": sorted(registered_names(config_path) & {"sellersprite", "sif", "xiyou", "apify"}),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="setup_mcp", description="跨境选品研究引擎一键部署")
    parser.add_argument("--engine", default=str(ROOT))
    parser.add_argument("--codex", action="store_true", help="注册 MCP 到 Codex(~/.codex/config.toml)")
    parser.add_argument("--codex-config", default=str(Path.home() / ".codex" / "config.toml"))
    parser.add_argument("--skip-venv", action="store_true")
    parser.add_argument("--skip-npm", action="store_true")
    parser.add_argument("--check", action="store_true", help="只体检,不做任何修改")
    args = parser.parse_args(argv)

    engine = Path(args.engine).resolve()
    config_path = Path(args.codex_config).expanduser()

    if args.check:
        print(json.dumps({"check": check_status(engine, config_path)}, ensure_ascii=False))
        return

    report: dict[str, object] = {}
    if not args.skip_venv:
        report["venv"] = run_step("venv", lambda: ensure_venv(engine))[1]
        print(f"[{report['venv'].split(':')[0]}] venv: {report['venv']}")
    if not args.skip_npm:
        report["npm"] = run_step("npm", ensure_npm_mcp)[1]
        print(f"[{report['npm'].split(':')[0]}] npm: {report['npm']}")
    created = ensure_env_files(engine)
    report["env_files"] = created
    for name, was_created in created.items():
        print(f"[{'ok' if was_created else 'skip'}] 文件 {name}: {'已创建' if was_created else '已存在,不覆盖'}")
    if args.codex:
        report["codex"] = run_step("codex", lambda: register_codex(engine, config_path))[1]
        for name, status in report["codex"].items():
            print(f"[{status.split(':')[0]}] codex/{name}: {status}")

    report["next_hint"] = "在 .env 填入 token 后重跑本脚本(--codex)完成注册;运行 --check 查看体检结果"
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
