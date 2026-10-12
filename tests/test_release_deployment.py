import json
import pytest
from fastapi.testclient import TestClient
from research_workflow.api import create_app
from research_workflow.store import SQLiteStore
from research_workflow.workflow import WorkflowService
from scripts import setup_mcp as setup


def test_api_denies_requests_without_operator_secret(monkeypatch):
    monkeypatch.delenv('RESEARCH_API_TOKEN', raising=False)
    client = TestClient(create_app(WorkflowService(SQLiteStore())))
    assert client.post('/projects', json={'product':'Ice','marketplace':'US'}).status_code == 503


def test_api_requires_token_and_local_peer(monkeypatch):
    monkeypatch.setenv('RESEARCH_API_TOKEN', 'test-only-secret')
    app = create_app(WorkflowService(SQLiteStore()))
    client = TestClient(app, client=('127.0.0.1', 50000))
    assert client.get('/docs').status_code == 401
    assert client.get('/docs', headers={'Authorization':'Bearer wrong'}).status_code == 401
    assert client.get('/docs', headers={'Authorization':'Bearer test-only-secret'}).status_code == 200
    remote = TestClient(app, client=('203.0.113.5', 50000))
    assert remote.get('/docs', headers={'Authorization':'Bearer test-only-secret'}).status_code == 403


def test_setup_failure_has_nonzero_exit_and_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, 'ensure_venv', lambda _: 'fail: fixture')
    monkeypatch.setattr(setup, 'ensure_env_files', lambda _: {})
    with pytest.raises(SystemExit) as exc:
        setup.main(['--engine',str(tmp_path),'--skip-npm'])
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])['venv'].startswith('fail')


def test_setup_codex_exception_does_not_crash_items(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, 'ensure_env_files', lambda _: {})
    def fail(*args):
        raise OSError('synthetic failure')
    monkeypatch.setattr(setup, 'register_codex', fail)
    with pytest.raises(SystemExit) as exc:
        setup.main(['--engine',str(tmp_path),'--skip-venv','--skip-npm','--codex'])
    assert exc.value.code == 1
    assert 'codex' in json.loads(capsys.readouterr().out.splitlines()[-1])


def test_custom_codex_config_is_respected_for_stdio(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.shutil,'which',lambda _: '/fake/codex')
    monkeypatch.setattr(setup.subprocess,'run',lambda *a,**k: pytest.fail('must not mutate implicit global config'))
    target = tmp_path / 'isolated' / 'config.toml'
    result = setup.register_codex(tmp_path,target)
    assert result['sellersprite'].startswith('ok')
    assert '[mcp_servers.sellersprite]' in target.read_text()
