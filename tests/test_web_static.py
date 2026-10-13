"""Web 工作台静态前端:CSP 兼容性与红线静态扫描。

前端是无构建链的原生 JS;这里锁定三件事:
1. 页面与静态资源可取,且响应带 CSP;
2. 无内联脚本 / 内联事件 / 外链资源 / innerHTML(防 XSS);
3. 前端文案不出现商业结论(最终决定由人工填写)。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from research_workflow.web import create_web_app, security

STATIC = Path(__file__).resolve().parent.parent / "src" / "research_workflow" / "web" / "static"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "PBKDF2_ITERATIONS", 1000)
    app = create_web_app(db_path=tmp_path / "research.db", root=tmp_path / "projects",
                         web_db_path=tmp_path / "web.db", secure_cookies=False)
    return TestClient(app)


def test_index_and_assets_are_served_with_csp(client) -> None:
    index = client.get("/")
    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    assert '<script src="/static/app.js"></script>' in index.text
    assert 'href="/static/style.css"' in index.text
    for path, kind in (("/static/app.js", "javascript"), ("/static/style.css", "css")):
        resp = client.get(path)
        assert resp.status_code == 200 and kind in resp.headers["content-type"]
        assert "default-src 'self'" in resp.headers["content-security-policy"]
    assert "default-src 'self'" in index.headers["content-security-policy"]


def test_index_has_no_inline_script_handlers_or_external_resources() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.S | re.I)
    assert scripts, "必须通过外部 /static/app.js 加载"
    for attrs, body in scripts:
        assert "src=" in attrs and not body.strip(), f"禁止内联脚本: {attrs} {body[:40]}"
    assert not re.search(r"\son[a-z]+\s*=", html, flags=re.I), "禁止内联事件处理器"
    assert not re.search(r"javascript:", html, flags=re.I)
    assert not re.search(r"<style\b", html, flags=re.I) and not re.search(r"\sstyle\s*=", html, flags=re.I)
    assert not re.search(r"(?:src|href)\s*=\s*[\"']?(?:https?:)?//", html, flags=re.I), "禁止外链资源"


def test_assets_have_no_external_references() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"https?://|@import|url\(\s*[\"']?(?!data:)", css, flags=re.I)
    assert not re.search(r"https?://", js, flags=re.I)


def test_app_js_never_builds_html_from_data() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in js, f"app.js 禁止使用 {banned}"


def test_frontend_has_no_business_verdict_wording() -> None:
    """红线:系统不产出商业结论,也不提供结论性按钮/文案。"""
    for name in ("app.js", "index.html"):
        text = (STATIC / name).read_text(encoding="utf-8").lower()
        for banned in ("建议进入", "yes", "不建议"):
            assert banned not in text, f"{name} 含禁用字样 {banned!r}"


def test_frontend_states_human_decision_boundary_and_never_auto_approves() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "最终商业决定由人工在报告第 8 节填写,系统不代填" in js
    # resolve 作业只在按钮点击回调里提交:全文件 approve 的取值只能来自字面量 true/false
    for match in re.finditer(r"approve:\s*([^,}\s]+)", js):
        assert match.group(1) in {"true", "false"}, f"approve 不得由变量决定: {match.group(0)}"
    assert js.count('startJob("resolve"') == 2  # 仅「确认」「驳回」两个按钮


def test_table_import_export_entry_never_approves_and_warns_about_invalidation() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "修改并上传后旧审批立即失效,需重新人工确认" in js
    assert 'startJob("export-table"' in js and 'startJob("import-table"' in js
    # 回传表只提交 import-table;批准动作仍只有 resolve 的两处按钮
    assert js.count('startJob("resolve"') == 2
    assert "approve" not in re.search(r'startJob\("import-table".*', js).group(0)
    for table in ("[1]", "[3, 2]", "[2]"):
        assert f"tablesBlock({table})" in js
