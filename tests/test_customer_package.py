"""客户安装包(Windows):构建白名单、不含密钥/内部文件、可复现;安装后配置与体检幂等且不泄露凭据。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))  # packaging/ 不是 Python 包,直接按模块导入构建脚本
import build_customer_package as pkg  # noqa: E402
# MCP SDK 在 import 时把当时的 sys.stderr 固化为 stdio 子进程的默认 errlog。体检(run_check)会在 capsys 生效期间
# 首次导入它,之后其它测试的 stdio 传输就拿到失效的捕获流(fileno 报错)。提前在收集阶段导入即可避免这个测试间串扰。
import mcp.client.stdio  # noqa: E402,F401

from scripts import install_customer as inst  # noqa: E402

TOP = pkg.PACKAGE_DIR + "/"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return pkg.build(tmp_path_factory.mktemp("dist"))


def _names(target: Path) -> set[str]:
    return {n.removeprefix(TOP) for n in zipfile.ZipFile(target).namelist()}


# ------------------------------------------------------------------ 构建


def test_package_contains_the_customer_files(built) -> None:
    names = _names(built[0])
    for required in ("install.bat", "install.ps1", "start.bat", "check.bat", "README.md", "USER_GUIDE.md", "INSTALL_FOR_CODEX.md",
                     "pyproject.toml", "uv.lock", ".env.example", "config/mcp.example.json", "scripts/install_customer.py",
                     "scripts/setup_mcp.py", "src/research_workflow/cli.py", "src/research_workflow/web/static/app.js", "VERSION"):
        assert required in names, required
    # 除根目录文件外,只有这四类目录
    assert all("/" not in n or n.startswith(("src/research_workflow/", "skill/", "scripts/", "config/")) for n in names)


def test_package_excludes_tests_fixtures_internal_docs_and_real_config(built) -> None:
    names = _names(built[0])
    banned_prefixes = ("tests/", "fixtures/", "docs/", ".github/", "runtime/", "dist/", ".venv/")
    banned_files = {".env", "config/mcp.json", "AGENTS.md", "CLAUDE.md", "DEVELOPMENT_STATUS.md", "session.json"}
    assert not [n for n in names if n.startswith(banned_prefixes) or n in banned_files]
    assert not [n for n in names if "__pycache__" in n or n.endswith((".pyc", ".db"))]


def test_package_has_no_secrets_and_scanner_catches_planted_ones(built) -> None:
    with zipfile.ZipFile(built[0]) as archive:
        files = {n: archive.read(n) for n in archive.namelist()}
    assert pkg.scan_for_secrets(files) == []
    planted = {"x.txt": b"token = sk-" + b"a" * 30, "y.txt": b"Authorization: Bearer " + b"Z" * 40,
               "z.txt": b"https://mcp.example.com/mcp?secret-key=" + b"0123456789abcdef0123",
               "w.txt": b"apify_api_" + b"A" * 30}
    assert {h.split(":")[0] for h in pkg.scan_for_secrets(planted)} == set(planted)


def test_build_refuses_to_package_when_a_secret_is_present(monkeypatch, tmp_path) -> None:
    real = pkg.collect_files

    def leaky():
        files = real()
        files["config/leak.json"] = b'{"k": "sk-' + b"b" * 30 + b'"}'
        return files

    monkeypatch.setattr(pkg, "collect_files", leaky)
    with pytest.raises(SystemExit, match="疑似密钥"):
        pkg.build(tmp_path)
    assert not list(tmp_path.glob("*.zip"))  # 拒绝时不留半成品


def test_windows_scripts_use_crlf_and_are_ascii_only(built) -> None:
    with zipfile.ZipFile(built[0]) as archive:
        for name in ("install.bat", "install.ps1", "start.bat", "check.bat"):
            data = archive.read(TOP + name)
            assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b""), name
            data.decode("ascii")  # 中文放进 .bat/.ps1 会在不同代码页/PowerShell 5.1 下乱码,脚本只用英文


def test_installer_uses_locked_dependencies_existing_extras_and_local_only_start() -> None:
    ps1 = (ROOT / "packaging/templates/install.ps1").read_text(encoding="utf-8")
    assert "sync --frozen" in ps1
    extras = set(tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"])
    wanted = set(re.findall(r"--extra (\w+)", ps1))
    assert wanted and wanted <= extras
    for needed in ("-NonInteractive", "-Check", "-AdminPasswordEnv"):
        assert needed in ps1
    assert "XUANPIN_NO_PAUSE" in (ROOT / "packaging/templates/install.bat").read_text(encoding="utf-8")
    start = (ROOT / "packaging/templates/start.bat").read_text(encoding="utf-8")
    assert "127.0.0.1" in start and "0.0.0.0" not in start  # 只监听本机


def test_build_is_reproducible_and_checksum_matches(built, tmp_path) -> None:
    target, checksum = built
    again, _ = pkg.build(tmp_path)
    assert again.read_bytes() == target.read_bytes()
    digest, name = checksum.read_text(encoding="utf-8").split()
    assert name == target.name and digest == hashlib.sha256(target.read_bytes()).hexdigest()


def test_pyproject_readme_exists_in_package(built) -> None:
    # hatchling 构建要求 pyproject 声明的 readme 文件存在
    readme = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["readme"]
    assert readme in _names(built[0])


def test_customer_docs_state_the_boundary_and_the_human_decision_rule(built) -> None:
    with zipfile.ZipFile(built[0]) as archive:
        readme = archive.read(TOP + "README.md").decode("utf-8")
        guide = archive.read(TOP + "USER_GUIDE.md").decode("utf-8")
        codex = archive.read(TOP + "INSTALL_FOR_CODEX.md").decode("utf-8")
    assert "尚未用真实" in readme and "不给 Yes/No" in readme
    assert "不会自动通过" in guide and "没有默认值" in guide
    assert "不要" in codex and "token" in codex and "自动批准检查点" in codex


# ------------------------------------------------------------------ 安装后配置


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCT_RESEARCH_ENGINE", str(tmp_path))
    for key in ("RESEARCH_DB_PATH", "RESEARCH_PROJECTS_ROOT", "RESEARCH_WEB_DB_PATH", "SIF_MCP_TOKEN", "XIYOU_MCP_TOKEN",
                "APIFY_TOKEN", "ANTHROPIC_API_KEY", "XUANPIN_TEST_PW"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env.example").write_text("SIF_MCP_TOKEN=\nXIYOU_MCP_TOKEN=\nAPIFY_TOKEN=\n", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "mcp.example.json").write_text("{}", encoding="utf-8")
    return tmp_path


def _report(out: str) -> dict:
    return json.loads(out[: out.rindex("}") + 1])


def test_generated_admin_password_goes_to_a_private_file_and_is_never_returned(root) -> None:
    result = inst.ensure_admin(root, "admin", None, interactive=False)
    assert result["status"] == "created" and result["password_source"] == "generated"
    path = root / "runtime" / inst.PASSWORD_FILE
    password = re.search(r"口令: (\S+)", path.read_text(encoding="utf-8")).group(1)
    assert len(password) >= 12 and password not in json.dumps(result)
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0
    # 生成的口令确实对应入库的哈希
    from research_workflow.web import security

    user = inst._web_store(root).get_user_by_name("admin")
    assert user["role"] == "admin" and security.verify_password(password, user["password_hash"])


def test_admin_setup_is_idempotent_and_never_overwrites(root) -> None:
    inst.ensure_admin(root, "admin", None, interactive=False)
    path = root / "runtime" / inst.PASSWORD_FILE
    before = path.read_text(encoding="utf-8")
    assert inst.ensure_admin(root, "admin", None, interactive=False) == {"username": "admin", "status": "exists"}
    assert path.read_text(encoding="utf-8") == before


def test_admin_password_from_environment_is_used_not_written_and_weak_is_rejected(root, monkeypatch) -> None:
    monkeypatch.setenv("XUANPIN_TEST_PW", "short")
    with pytest.raises(ValueError, match="至少"):
        inst.ensure_admin(root, "admin", "XUANPIN_TEST_PW", interactive=False)
    assert inst._web_store(root).get_user_by_name("admin") is None  # 失败不留半个账号
    monkeypatch.setenv("XUANPIN_TEST_PW", "a-long-enough-password-1")
    result = inst.ensure_admin(root, "admin", "XUANPIN_TEST_PW", interactive=False)
    assert result["password_source"] == "environment" and "password_file" not in result
    assert not (root / "runtime" / inst.PASSWORD_FILE).exists()


def test_check_mode_changes_nothing_and_never_echoes_credentials(root, capsys) -> None:
    (root / ".env").write_text("SIF_MCP_TOKEN=super-secret-token-value-123\n", encoding="utf-8")
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    inst.main(["--root", str(root), "--check"])
    out = capsys.readouterr().out
    assert "super-secret-token-value-123" not in out
    filled = _report(out)["check"]["data_source_credentials_filled"]
    assert filled["SIF"] is True and filled["Apify"] is False
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == before  # --check 不改任何文件


def test_full_install_creates_skeleton_and_smoke_starts_the_real_server(root, capsys) -> None:
    code = inst.main(["--root", str(root), "--non-interactive"])
    report = _report(capsys.readouterr().out)
    assert code == 0 and report["ok"] is True
    assert report["smoke"] == {"ok": True, "api_me_status": 401, "home_status": 200}  # 未登录 401 = 鉴权在位
    assert (root / ".env").is_file() and (root / "config" / "mcp.json").is_file() and (root / "runtime" / "projects").is_dir()
    env_text = (root / ".env").read_text(encoding="utf-8")
    inst.main(["--root", str(root), "--non-interactive", "--no-smoke"])  # 再跑一遍:不重复创建、不覆盖
    capsys.readouterr()
    assert (root / ".env").read_text(encoding="utf-8") == env_text
