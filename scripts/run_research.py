#!/usr/bin/env python3
"""兼容入口:CLI 实现位于 research_workflow.cli。

本模块把自身替换为 research_workflow.cli,使 `from scripts.run_research import main`
以及对 `_build_gateway` 等函数的 monkeypatch 仍作用于同一份实现。
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from research_workflow import cli  # noqa: E402

if __name__ == "__main__":
    cli.main()
else:
    sys.modules[__name__] = cli
