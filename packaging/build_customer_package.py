#!/usr/bin/env python3
"""构建客户安装包(Windows):dist/xuanpin-research-<版本>-windows.zip + .sha256

只打包白名单文件:引擎源码、skill、锁文件、安装/启动脚本与客户文档。
不含:测试、fixtures、内部开发文档、真实 .env / config/mcp.json、runtime 数据。
构建前后都会扫描密钥特征,发现疑似密钥直接失败。同一份提交重复构建得到字节相同的 zip(固定时间戳、固定顺序)。

  python packaging/build_customer_package.py [--output dist] [--version-suffix ""]
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "packaging" / "templates"
PACKAGE_DIR = "xuanpin-research"
ZIP_TIME = (2026, 1, 1, 0, 0, 0)

# (源路径, 包内路径);目录整体递归
INCLUDE_TREES = [("src/research_workflow", "src/research_workflow"), ("skill", "skill")]
INCLUDE_FILES = [
    ("pyproject.toml", "pyproject.toml"), ("uv.lock", "uv.lock"), ("LICENSE", "LICENSE"),
    (".env.example", ".env.example"), ("config/mcp.example.json", "config/mcp.example.json"),
    ("scripts/__init__.py", "scripts/__init__.py"), ("scripts/run_research.py", "scripts/run_research.py"),
    ("scripts/setup_mcp.py", "scripts/setup_mcp.py"), ("scripts/install_customer.py", "scripts/install_customer.py"),
]
# 模板 -> 包根目录;.bat/.ps1 统一 CRLF(Windows 批处理对 LF 换行不可靠)
TEMPLATE_FILES = ["README.md", "INSTALL_FOR_CODEX.md", "USER_GUIDE.md", "install.bat", "install.ps1", "start.bat", "check.bat"]
CRLF_SUFFIXES = {".bat", ".ps1"}
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".venv"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".db", ".db-journal"}

# 疑似密钥:命中即构建失败(占位符/示例变量名不会命中)
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"), re.compile(r"\bapify_api_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-.]{24,}"), re.compile(r"(?i)secret-key=[A-Za-z0-9]{16,}"),
    re.compile(r"\bmcp_[0-9a-f]{24,}"), re.compile(r"\bsifmcp[A-Za-z0-9]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def git_short_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short=10", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _iter_tree(src: Path, dest: str):
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if not path.is_file() or SKIP_PARTS & set(rel.parts) or path.suffix in SKIP_SUFFIXES:
            continue
        yield path, f"{dest}/{rel.as_posix()}"


def collect_files() -> dict[str, bytes]:
    """包内路径 -> 内容(已处理换行);路径按字典序固定。"""
    files: dict[str, bytes] = {}
    for src, dest in INCLUDE_TREES:
        for path, arcname in _iter_tree(ROOT / src, dest):
            files[arcname] = path.read_bytes()
    for src, dest in INCLUDE_FILES:
        path = ROOT / src
        if not path.is_file():
            raise FileNotFoundError(f"安装包缺少必需文件: {src}")
        files[dest] = path.read_bytes()
    for name in TEMPLATE_FILES:
        files[name] = (TEMPLATES / name).read_bytes()
    for name, data in list(files.items()):
        if Path(name).suffix in CRLF_SUFFIXES:
            files[name] = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    files["VERSION"] = f"xuanpin-research {project_version()}\ncommit {git_short_sha()}\n".encode("utf-8")
    return dict(sorted(files.items()))


def scan_for_secrets(files: dict[str, bytes]) -> list[str]:
    hits = []
    for name, data in files.items():
        if b"\x00" in data[:2048]:
            continue
        text = data.decode("utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                hits.append(f"{name}: {pattern.pattern}")
    return hits


def build(output_dir: Path, version_suffix: str = "") -> tuple[Path, Path]:
    files = collect_files()
    hits = scan_for_secrets(files)
    if hits:
        raise SystemExit("拒绝打包:发现疑似密钥\n  " + "\n  ".join(hits))
    output_dir.mkdir(parents=True, exist_ok=True)
    name = f"xuanpin-research-{project_version()}{version_suffix}-windows.zip"
    target = output_dir / name
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for arcname, data in files.items():
            info = ZipInfo(f"{PACKAGE_DIR}/{arcname}", date_time=ZIP_TIME)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    checksum = target.with_suffix(".zip.sha256")
    checksum.write_text(f"{digest}  {name}\n", encoding="utf-8")
    return target, checksum


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建客户安装包(Windows)")
    parser.add_argument("--output", default=str(ROOT / "dist"))
    parser.add_argument("--version-suffix", default="", help="追加到版本号后的后缀,如 -rc1")
    args = parser.parse_args(argv)
    target, checksum = build(Path(args.output), args.version_suffix)
    print(f"已生成: {target}\nSHA256: {checksum.read_text(encoding='utf-8').split()[0]}\n校验文件: {checksum}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
