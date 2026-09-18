from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

import pytest

from propertyai_core.tests import fixture_lifecycle as life
from propertyai_core.tests import postgres_stage_a_cluster as pg


@pytest.mark.parametrize('phase', ['INIT', 'START', 'READY', 'RUN'])
def test_fixture_setup_failures_are_infra_and_owned_partial_root_is_removed(monkeypatch, phase):
    original = pg._run_command
    def injected(command, **kwargs):
        name = Path(command[0]).name
        match = ((phase == 'INIT' and name == 'initdb') or
                 (phase == 'START' and name == 'pg_ctl' and command[-1] == 'start') or
                 (phase == 'READY' and name == 'psql' and command[-1] == 'SELECT 1') or
                 (phase == 'RUN' and name == 'flyway' and command[-1] == 'migrate'))
        if match:
            if phase == 'INIT':
                data = Path(command[command.index('-D') + 1]); data.mkdir()
                (data/'partial').write_text('partially created initdb cluster')
            raise subprocess.CalledProcessError(23, command, stderr='injected fixture failure')
        return original(command, **kwargs)
    monkeypatch.setattr(pg, '_run_command', injected)
    with pytest.raises(life.FixtureLifecycleError) as error:
        pg.start_disposable_postgres()
    assert error.value.phase == phase
    assert error.value.classification == 'INFRA_ERROR'
    report = error.value.report
    assert report['ownership'] == 'KNOWN' and report['residual_checked']
    assert report['root_absent'] and report['cleanup_ok']
    assert report['failures'] and not Path(report['root']).exists()
    assert life.classify_outcome('PASS', report) == 'INFRA_ERROR'


def test_missing_fixture_binary_is_infra_not_skip(monkeypatch):
    def unavailable(name): pytest.skip('injected missing binary')
    monkeypatch.setattr(pg, '_postgres_bin', unavailable)
    with pytest.raises(life.FixtureLifecycleError, match='INFRA_ERROR:INIT:CAPABILITY_UNAVAILABLE'):
        pg.start_disposable_postgres()


def test_actual_fixture_lifecycle_and_dcs_artifacts_have_positive_cleanup_proof():
    cluster = pg.start_disposable_postgres()
    try:
        phases = [row['phase'] for row in cluster.lifecycle.observations if row['result'] == 'PASS']
        assert phases == ['INIT', 'START', 'READY', 'RUN']
        (cluster.root/'test-owned-dcs.sqlite3').write_bytes(b'synthetic owned DCS artifact')
        (cluster.root/'test-owned-dcs.sqlite3-wal').write_bytes(b'synthetic WAL artifact')
        assert cluster.lifecycle._postmasters()
    finally:
        report = cluster.cleanup()
    assert report['classification'] == 'PASS'
    assert report['ownership'] == 'KNOWN' and report['termination_attempted']
    assert report['stop_observed'] and report['residual_checked']
    assert report['postmaster_pids'] == [] and report['runtime_residuals'] == []
    assert report['root_absent'] and not cluster.root.exists()
    assert life.classify_outcome('PASS', report) == 'PASS'


@pytest.fixture
def owned():
    owner = life.FixtureLifecycle.create()
    (owner.root/'data').mkdir(); (owner.root/'sock').mkdir()
    try:
        yield owner
    finally:
        # Each injected mutation must first be restored. Never broad-delete unknown resources.
        if owner.root.exists():
            owner.guard()
            report = owner.finish(pg._postgres_bin('pg_ctl'))
            assert report['root_absent'], report


@pytest.mark.parametrize('damage', ['wrong-token', 'missing-marker', 'child-symlink', 'foreign-pid'])
def test_ambiguous_ownership_never_deletes_or_signals_foreign_resources(owned, monkeypatch, tmp_path, damage):
    marker = owned.root/life.MARKER
    original = marker.read_text()
    foreign = tmp_path/'foreign'; foreign.mkdir(); (foreign/'keep').write_text('KEEP')
    if damage == 'wrong-token':
        data = json.loads(original); data['nonce'] = 'wrong'; marker.write_text(json.dumps(data))
    elif damage == 'missing-marker': marker.unlink()
    elif damage == 'child-symlink':
        (owned.root/'data').rmdir(); (owned.root/'data').symlink_to(foreign, target_is_directory=True)
    elif damage == 'foreign-pid':
        (owned.root/'data/postmaster.pid').write_text(f'{os.getpid()}\n{owned.root / "data"}\n')
    try:
        report = owned.finish(pg._postgres_bin('pg_ctl'))
        assert report['classification'] == 'INCOMPLETE'
        assert not report['termination_attempted'] and not report['cleanup_ok']
        assert owned.root.exists() and (foreign/'keep').read_text() == 'KEEP'
        assert life.classify_outcome('PASS', report) == 'INCOMPLETE'
    finally:
        if (owned.root/'data').is_symlink():
            (owned.root/'data').unlink(); (owned.root/'data').mkdir()
        marker.write_text(original)
        (owned.root/'data/postmaster.pid').unlink(missing_ok=True)


@pytest.mark.parametrize('residual', ['stale-socket', 'stale-pid', 'cleanup-error', 'residual-root', 'process-check-unavailable'])
def test_cleanup_faults_cannot_be_false_pass(owned, monkeypatch, residual):
    sock = None
    if residual == 'stale-socket':
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(owned.root/'sock/.s.PGSQL.12345'))
    elif residual == 'stale-pid':
        dead = subprocess.Popen([sys.executable, '-c', 'pass']); dead.wait(timeout=5)
        (owned.root/'data/postmaster.pid').write_text(f'{dead.pid}\n{owned.root / "data"}\n')
    with monkeypatch.context() as patch:
        if residual == 'cleanup-error':
            def denied(root): raise PermissionError('injected cleanup error')
            patch.setattr(life.shutil, 'rmtree', denied)
        elif residual == 'residual-root': patch.setattr(life.shutil, 'rmtree', lambda root: None)
        elif residual == 'process-check-unavailable':
            def unavailable(): raise OSError('injected ps unavailable')
            patch.setattr(owned, '_postmasters', unavailable)
        report = owned.finish(pg._postgres_bin('pg_ctl'))
    if sock: sock.close()
    assert report['classification'] in {'INFRA_ERROR', 'INCOMPLETE'}
    assert life.classify_outcome('PASS', report) != 'PASS'
    if residual in {'cleanup-error','residual-root','process-check-unavailable'}:
        assert owned.root.exists()
    else:
        assert report['residual_checked'] and report['runtime_residuals'] and report['root_absent']


def test_actual_postgres_stop_failure_retains_cluster_and_is_incomplete(monkeypatch):
    cluster = pg.start_disposable_postgres()
    real = life.run_owned_command
    try:
        with monkeypatch.context() as patch:
            def failed_stop(command, **kwargs):
                if command[-1] == 'stop': return subprocess.CompletedProcess(command, 19, '', 'injected stop failure')
                return real(command, **kwargs)
            patch.setattr(life, 'run_owned_command', failed_stop)
            with pytest.raises(life.FixtureLifecycleError) as error:
                cluster.cleanup()
            report = error.value.report
            assert report['classification'] == 'INCOMPLETE'
            assert report['termination_attempted'] and not report['stop_observed']
            assert cluster.root.exists() and report['postmaster_pids']
    finally:
        # Retry the exact known test-owned stop, preserving the first failure in evidence.
        result = cluster.lifecycle.finish(pg._postgres_bin('pg_ctl'))
        assert result['root_absent'] and result['cleanup_ok']
        assert result['classification'] == 'INFRA_ERROR'


@pytest.mark.parametrize('mode', ['timeout', 'leaked-child'])
def test_owned_command_timeout_and_child_leak_are_not_pass(mode):
    if mode == 'timeout':
        code = 'import time; time.sleep(60)'
        timeout = 0.1
    else:
        code = ('import subprocess,sys; '
                'subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],'
                'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)')
        timeout = 5
    with pytest.raises(life.FixtureLifecycleError) as error:
        life.run_owned_command([sys.executable, '-c', code], timeout=timeout)
    assert error.value.classification == 'INFRA_ERROR'
    assert error.value.reason == ('COMMAND_TIMEOUT' if mode == 'timeout' else 'CHILD_PROCESS_LEAK')
    assert error.value.report['termination_attempted']
    assert error.value.report['residual_checked'] and error.value.report['process_group_absent']
    assert error.value.report['parent_reaped']


@pytest.mark.parametrize('test_result, expected', [('PASS','PASS'), ('FAIL','TEST_FAILURE'),
    ('TIMEOUT','INFRA_ERROR'), ('SKIP','INCOMPLETE'), ('XFAIL','INCOMPLETE'), ('MISSING','INCOMPLETE'),
    ('NOT_COLLECTED','INCOMPLETE'), ('INCOMPLETE','INCOMPLETE'), ('SEMANTIC_BINDING_DRIFT','INCOMPLETE')])
def test_fixture_result_classification_is_explicit(test_result, expected):
    clean = {'ownership':'KNOWN','residual_checked':True,'classification':'PASS','cleanup_ok':True,'failures':[]}
    assert life.classify_outcome(test_result, clean) == expected
    assert life.classify_outcome(test_result, None) == 'INCOMPLETE'


def test_fixture_subprocess_env_drops_ambient_production_configuration(monkeypatch):
    for key in ['PGHOST','PGPASSWORD','PGSERVICE','PGOPTIONS','ADCP_CONTROL_STORE','NOTION_TOKEN','TELEGRAM_BOT_TOKEN']:
        monkeypatch.setenv(key, 'must-not-inherit')
        assert key not in life.safe_fixture_env()

    # The current runner's accepted wheel must also reach its owned W07 children;
    # otherwise the parent could bind one source while the child imports site-packages.
    from propertyai_core.tests.batch_a_failure_harness import safe_env
    monkeypatch.setenv("PROPERTYAI_ADCP_GLOBAL_WRITER_CLIENT_WHEEL", "/test/accepted.whl")
    child_env = safe_env(Path("/tmp"))
    assert child_env["PYTHONPATH"].endswith(os.pathsep + "/test/accepted.whl")
    assert child_env["PROPERTYAI_ADCP_GLOBAL_WRITER_CLIENT_WHEEL"] == "/test/accepted.whl"
    assert "PGPASSWORD" not in child_env and "TELEGRAM_BOT_TOKEN" not in child_env
