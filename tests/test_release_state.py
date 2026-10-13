"""Release regressions: local files and simulated human responses only."""
import json
from pathlib import Path

import pytest

from scripts.run_research import main
from research_workflow.store import SQLiteStore
from research_workflow.workflow import WorkflowService
from test_cli_research import CLI, CSV_2025


@pytest.fixture
def cli(tmp_path, capsys):
    return CLI(tmp_path, capsys)


def ingest(cli, tmp_path):
    pid = cli.run('init', '--product', 'Ice Maker', '--marketplace', 'US')['project_id']
    path = tmp_path / '2025-01.csv'
    path.write_text(CSV_2025)
    cp = cli.run('ingest', '--project', pid, '--file', str(path))['checkpoint_id']
    return pid, path, cp


def approve(cli, pid, cp):
    return cli.run('resolve', '--project', pid, '--checkpoint', cp, '--approve')


def classify(cli, pid):
    return cli.run('classify', '--project', pid, '--label', 'nugget=Nugget', '--rule', 'nugget=nugget')


def ready(cli, tmp_path):
    pid, path, cp = ingest(cli, tmp_path)
    approve(cli, pid, cp)
    approve(cli, pid, classify(cli, pid)['checkpoint_id'])
    approve(cli, pid, cli.run('analyze', '--project', pid)['checkpoint_id'])
    return pid, path


def test_first_init_creates_database_parent(tmp_path, capsys):
    main(['--db', str(tmp_path / 'new/runtime/research.db'), '--root', str(tmp_path / 'projects'),
          'init', '--product', 'Ice', '--marketplace', 'US'])
    assert json.loads(capsys.readouterr().out)['project_id']


def test_classification_requires_data_approval(cli, tmp_path):
    pid, path, cp = ingest(cli, tmp_path)
    with pytest.raises(SystemExit):
        classify(cli, pid)
    cli.run('resolve', '--project', pid, '--checkpoint', cp, '--reject')
    assert cli.run('status', '--project', pid)['next'] == 'ingest'
    with pytest.raises(SystemExit):
        classify(cli, pid)


def test_old_checkpoint_cannot_approve_new_classification(cli, tmp_path):
    pid, path, cp = ingest(cli, tmp_path)
    approve(cli, pid, cp)
    old = classify(cli, pid)['checkpoint_id']
    new = classify(cli, pid)['checkpoint_id']
    with pytest.raises(SystemExit):
        approve(cli, pid, old)
    assert cli.run('status', '--project', pid)['pending_checkpoint']['checkpoint_id'] == new
    envelope = json.loads((tmp_path / 'projects' / pid / 'classification.json').read_text())
    assert not envelope['batch']['approved']


def test_reingest_invalidates_approved_downstream(cli, tmp_path):
    pid, path = ready(cli, tmp_path)
    cli.run('dossier', '--project', pid, '--output', str(tmp_path / 'dossier'))
    path.write_text(CSV_2025.replace('N1,20', 'N1,999'))
    cli.run('ingest', '--project', pid, '--file', str(path))
    for command in ['analyze', 'dossier']:
        args = ['--output', str(tmp_path / 'bad')] if command == 'dossier' else []
        with pytest.raises(SystemExit):
            cli.run(command, '--project', pid, *args)
    assert 'dossier' not in cli.run('status', '--project', pid)['state']['completed_steps']


def test_changed_data_after_approval_requires_reapproval(cli, tmp_path):
    pid, path, cp = ingest(cli, tmp_path)
    approve(cli, pid, cp)
    records = tmp_path / 'projects' / pid / 'records.json'
    payload = json.loads(records.read_text())
    payload['2025-01'][0]['monthly_sales'] = 999
    records.write_text(json.dumps(payload))
    with pytest.raises(SystemExit):
        classify(cli, pid)


def test_reanalyze_drops_omitted_new_products(cli, tmp_path):
    pid, path = ready(cli, tmp_path)
    new = tmp_path / 'new.csv'
    new.write_text('ASIN,month,monthly_sales\nN1,2025-01,500\n')
    cp = cli.run('analyze', '--project', pid, '--new-products-file', str(new))['checkpoint_id']
    approve(cli, pid, cp)
    cp = cli.run('analyze', '--project', pid)['checkpoint_id']
    approve(cli, pid, cp)
    out = tmp_path / 'export'
    cli.run('dossier', '--project', pid, '--output', str(out))
    assert json.loads((out / 'data.json').read_text())['new_product_analysis'] is None


def test_service_rejects_superseded_checkpoint():
    svc = WorkflowService(SQLiteStore())
    pid = svc.create_project('Ice', 'US').id
    old = svc.request_human_checkpoint(pid, 'data_review', 'old', {})
    new = svc.request_human_checkpoint(pid, 'data_review', 'new', {})
    with pytest.raises(ValueError):
        svc.resolve_human_checkpoint(old.id, approved=True)
    assert svc.snapshot(pid)['state']['pending_checkpoint_id'] == new.id


def test_approved_absent_optional_artifact_cannot_be_added(cli, tmp_path):
    pid, _ = ready(cli, tmp_path)
    (tmp_path / 'projects' / pid / 'new_products.json').write_text('{}')
    with pytest.raises(SystemExit):
        cli.run('dossier', '--project', pid, '--output', str(tmp_path / 'unreviewed'))


def test_approval_database_failure_restores_bytes_and_can_retry(cli, tmp_path, monkeypatch):
    import sqlite3
    pid, path, cp = ingest(cli, tmp_path)
    approve(cli, pid, cp)
    cp = classify(cli, pid)['checkpoint_id']
    path = tmp_path / 'projects' / pid / 'classification.json'
    before = path.read_bytes()
    original = WorkflowService.resolve_human_checkpoint
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic failure')
    monkeypatch.setattr(WorkflowService, 'resolve_human_checkpoint', fail)
    with pytest.raises(SystemExit):
        approve(cli, pid, cp)
    assert path.read_bytes() == before
    monkeypatch.setattr(WorkflowService, 'resolve_human_checkpoint', original)
    assert approve(cli, pid, cp)['resolution'] == 'approved'


def test_cli_commands_are_serialized_between_processes(cli, tmp_path):
    import subprocess
    import sys
    pid, path = ready(cli, tmp_path)
    store = SQLiteStore(tmp_path / 'research.db')
    code = ('from scripts.run_research import main; print("READY", flush=True); '
            'main(' + repr([*cli.base, 'classify', '--project',pid,'--label','nugget=Nugget']) + ')')
    process = None
    try:
        with store.transaction():
            process = subprocess.Popen([sys.executable,'-c',code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert process.stdout.readline().strip() == 'READY'
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.2)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert json.loads(stdout)['checkpoint_step'] == 'classification_review'
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        store.close()


def test_ingest_missing_sales_blocks_market_claims(cli, tmp_path):
    pid, path, cp = ingest(cli, tmp_path)
    path.write_text('ASIN,title\nA,No sales evidence\n')
    with pytest.raises(SystemExit):
        cli.run('ingest','--project',pid,'--file',str(path))
    assert cli.run('status','--project',pid)['pending_checkpoint']['checkpoint_id'] == cp


@pytest.mark.parametrize('value', ['false', 'true', 1, {'value':True}])
def test_graph_and_service_require_explicit_boolean_approval(value):
    from research_workflow.workflow.full_graph import _is_approved
    assert not _is_approved({'approved':value})
    svc = WorkflowService(SQLiteStore())
    pid = svc.create_project('Ice','US').id
    cp = svc.request_human_checkpoint(pid,'data_review','review',{})
    with pytest.raises(ValueError):
        svc.resolve_human_checkpoint(cp.id, approved=value)


def test_corrupt_source_snapshot_is_rejected(tmp_path):
    from scripts.run_research import _snapshot_source
    from hashlib import sha256
    original = tmp_path / 'input.csv'
    original.write_text(CSV_2025)
    pdir = tmp_path / 'project'
    target = pdir / 'raw' / (sha256(original.read_bytes()).hexdigest()+'.csv')
    target.parent.mkdir(parents=True)
    target.write_text('partial download')
    with pytest.raises(ValueError, match='快照'):
        _snapshot_source(pdir,original)


def test_export_contains_all_approved_artifact_bytes(cli, tmp_path):
    from hashlib import sha256
    pid, path = ready(cli,tmp_path)
    out = tmp_path / 'verified-package'
    cli.run('dossier','--project',pid,'--output',str(out))
    approvals = json.loads((out/'evidence'/'approvals.json').read_text())
    for cp in approvals.values():
        for name, expected in cp['response']['artifact_hashes'].items():
            evidence = out/'evidence'/name
            assert (sha256(evidence.read_bytes()).hexdigest() if evidence.exists() else None) == expected
    for item in json.loads((out/'source_index.json').read_text())['sources']:
        assert sha256((out/item['source']).read_bytes()).hexdigest() == item['sha256']


def test_stopped_runtime_backup_restores_on_another_path(cli, tmp_path):
    import shutil
    pid, path = ready(cli,tmp_path)
    # CLI has closed its SQLite connection; simulate a stopped-runtime backup.
    restored = tmp_path / 'restored'
    restored.mkdir()
    shutil.copyfile(tmp_path/'research.db', restored/'research.db')
    shutil.copytree(tmp_path/'projects', restored/'projects')
    shutil.rmtree(tmp_path/'projects')
    (tmp_path/'research.db').unlink()
    path.unlink()
    cli.base = ['--db',str(restored/'research.db'),'--root',str(restored/'projects')]
    assert cli.run('status','--project',pid)['next'] == 'dossier'
    cli.run('dossier','--project',pid,'--output',str(restored/'dossier'))
    assert (restored/'dossier'/'evidence'/'approvals.json').is_file()
