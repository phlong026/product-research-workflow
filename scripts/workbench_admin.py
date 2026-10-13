#!/usr/bin/env python3
"""工作台用户管理（在服务器上执行；令牌只在创建/轮换时显示一次）。

  python scripts/workbench_admin.py create-user --name alice --role admin
  python scripts/workbench_admin.py list-users
  python scripts/workbench_admin.py rotate-token --name alice
  python scripts/workbench_admin.py disable-user --name bob
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from research_workflow.web.db import WorkbenchStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=os.environ.get("WORKBENCH_DB", str(ROOT / "runtime" / "workbench.db")))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-user"); p.add_argument("--name", required=True); p.add_argument("--role", choices=["admin", "member"], default="member")
    sub.add_parser("list-users")
    p = sub.add_parser("rotate-token"); p.add_argument("--name", required=True)
    p = sub.add_parser("disable-user"); p.add_argument("--name", required=True)
    p = sub.add_parser("enable-user"); p.add_argument("--name", required=True)
    args = parser.parse_args(argv)
    store = WorkbenchStore(args.db)
    if args.cmd == "create-user":
        user, token = store.create_user(args.name, args.role)
        print(f"用户 {user['name']}（{user['role']}）已创建。访问令牌（只显示这一次）:\n{token}")
    elif args.cmd == "list-users":
        for u in store.list_users():
            print(f"{u['name']}\t{u['role']}\t{'已停用' if u['disabled'] else '正常'}\t{u['created_at']}")
    elif args.cmd == "rotate-token":
        print(f"新令牌（只显示这一次，旧令牌立即失效）:\n{store.rotate_token(args.name)}")
    else:
        store.set_disabled(args.name, args.cmd == "disable-user")
        print("已更新")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
