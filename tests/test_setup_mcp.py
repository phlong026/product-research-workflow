"""setup_mcp.py 部署脚本的单元测试(不依赖 codex CLI / npm / 网络)。"""
from __future__ import annotations

from pathlib import Path

import scripts.setup_mcp as setup
from scripts.setup_mcp import (
    build_http_headers,
    ensure_env_files,
    parse_env_file,
    registered_names,
    register_codex,
    replace_toml_section,
    toml_section_http,
)


def test_parse_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 注释\n"
        "SIF_MCP_TOKEN=abc123\n"
        'XIYOU_MCP_TOKEN="quoted-token"\n'
        "export APIFY_TOKEN='single'\n"
        "EMPTY=\n"
        "INVALID LINE\n",
        encoding="utf-8",
    )
    parsed = parse_env_file(env_file)
    assert parsed == {
        "SIF_MCP_TOKEN": "abc123",
        "XIYOU_MCP_TOKEN": "quoted-token",
        "APIFY_TOKEN": "single",
    }
    assert parse_env_file(tmp_path / "missing.env") == {}


def test_build_http_headers() -> None:
    assert build_http_headers("secret-key", "", "tok") == {"secret-key": "tok"}
    assert build_http_headers("Authorization", "Bearer", "tok") == {
        "Authorization": "Bearer tok"
    }
    assert build_http_headers("", "Bearer", "tok") == {"Authorization": "Bearer tok"}


def test_replace_toml_section_append_and_replace(tmp_path: Path) -> None:
    original = 'model = "gpt"\n\n[features]\ngoals = true\n'
    section = toml_section_http("sif", "https://mcp.sif.com/mcp", {"secret-key": "tok"})
    updated = replace_toml_section(original, "sif", section)
    assert '[mcp_servers.sif]' in updated
    assert 'secret-key = "tok"' in updated
    assert 'goals = true' in updated and 'model = "gpt"' in updated  # 其余内容保留

    # 已存在(含子表)时整体替换,旧 token 不残留
    changed = toml_section_http("sif", "https://mcp.sif.com/mcp", {"secret-key": "NEW"})
    replaced = replace_toml_section(updated, "sif", changed)
    assert '"NEW"' in replaced and '"tok"' not in replaced
    assert replaced.count("[mcp_servers.sif]") == 1
    assert registered_names(tmp_path / "none.toml") == set()


def test_ensure_env_files_creates_once(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("X=1\n", encoding="utf-8")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "mcp.example.json").write_text("{}", encoding="utf-8")

    created = ensure_env_files(tmp_path)
    assert created == {".env": True, "config/mcp.json": True}
    # 手工修改 .env(如填入 token)后重跑,不得覆盖
    (tmp_path / ".env").write_text("X=1\nSIF_MCP_TOKEN=real-token\n", encoding="utf-8")
    again = ensure_env_files(tmp_path)
    assert again == {".env": False, "config/mcp.json": False}
    assert "real-token" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_register_codex_http_servers(tmp_path: Path, monkeypatch) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / ".env").write_text(
        "SIF_MCP_URL=https://sif.example/mcp\n"
        "SIF_MCP_AUTH_HEADER=secret-key\n"
        "SIF_MCP_AUTH_SCHEME=\n"
        "SIF_MCP_TOKEN=sif-token\n"
        "XIYOU_MCP_URL=https://xiyou.example/mcp\n"
        "XIYOU_MCP_AUTH_HEADER=Authorization\n"
        "XIYOU_MCP_AUTH_SCHEME=Bearer\n"
        "XIYOU_MCP_TOKEN=xiyou-token\n"
        # apify 无 token → 应跳过
        "APIFY_MCP_URL=https://mcp.apify.com\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt"\n', encoding="utf-8")
    monkeypatch.setattr(setup.shutil, "which", lambda _: None)  # 无 codex CLI 的机器

    result = register_codex(engine, config)
    assert result["sellersprite"].startswith("fail")  # 无 codex CLI,如实报告
    assert result["sif"].startswith("ok")
    assert result["xiyou"].startswith("ok")
    assert result["apify"].startswith("skip")

    text = config.read_text(encoding="utf-8")
    assert '[mcp_servers.sif.env_http_headers]' in text and '"secret-key" = "SIF_MCP_TOKEN"' in text
    assert '[mcp_servers.xiyou]' in text and 'bearer_token_env_var = "XIYOU_MCP_TOKEN"' in text
    assert 'sif-token' not in text and 'xiyou-token' not in text
    assert config.stat().st_mode & 0o777 == 0o600
    assert "api.example" not in text and "[mcp_servers.apify]" not in text
    assert 'model = "gpt"' in text
    assert (tmp_path / "config.toml.bak-xuanpin-setup").exists()  # 首次修改前有备份
    assert registered_names(config) == {"sif", "xiyou"}
