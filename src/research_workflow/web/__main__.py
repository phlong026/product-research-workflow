"""Web 工作台入口:

  python -m research_workflow.web create-user --username alice --role researcher   # 口令交互输入,不走命令行参数
  python -m research_workflow.web serve [--host 127.0.0.1 --port 8800]

口令只通过交互式 getpass 输入。默认只监听本机;对外提供服务须置于 HTTPS 反向代理之后。
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from . import security
from .store import WebStore


def _paths() -> tuple[Path, Path, Path]:
    engine_root = Path(os.environ.get("PRODUCT_RESEARCH_ENGINE") or Path(__file__).resolve().parents[3])
    runtime = engine_root / "runtime"
    return (Path(os.environ.get("RESEARCH_DB_PATH", runtime / "research.db")),
            Path(os.environ.get("RESEARCH_PROJECTS_ROOT", runtime / "projects")),
            Path(os.environ.get("RESEARCH_WEB_DB_PATH", runtime / "web.db")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research_workflow.web")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cu = sub.add_parser("create-user", help="创建账号(口令交互输入)")
    cu.add_argument("--username", required=True)
    cu.add_argument("--role", choices=["admin", "researcher"], default="researcher")
    sv = sub.add_parser("serve", help="启动 Web 工作台")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8800)
    sv.add_argument("--insecure-cookies", action="store_true", help="仅本机 http 调试时使用;生产必须 HTTPS")
    args = parser.parse_args(argv)
    db_path, root, web_db = _paths()

    if args.cmd == "create-user":
        password = getpass.getpass("口令(至少 12 个字符): ")
        if password != getpass.getpass("再次输入: "):
            print("两次口令不一致", file=sys.stderr)
            return 1
        try:
            security.validate_password_strength(password)
            WebStore(web_db).create_user(args.username, password, args.role)
        except ValueError as exc:
            print(f"创建失败: {exc}", file=sys.stderr)
            return 1
        print(f"已创建账号 {args.username} ({args.role})")
        return 0

    import uvicorn

    from .app import create_web_app

    app = create_web_app(db_path=db_path, root=root, web_db_path=web_db, secure_cookies=not args.insecure_cookies)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
