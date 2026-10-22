"""Retry/failure cleanup must prove shutdown before touching installation files."""
import base64
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import pytest
from tests.test_installer_retry_staging import make_installer
from deployment.installer.platforms.windows import WindowsPlatformAdapter


def test_archive_stops_before_moving_and_failure_preserves_files(tmp_path, monkeypatch):
    installer = make_installer(tmp_path)
    monkeypatch.setattr(installer.state_machine, 'transition', lambda *a, **kw: None)
    calls = []
    monkeypatch.setattr(installer.platform, 'quiesce_installation', lambda *a: calls.append('stop'), raising=False)
    monkeypatch.setattr(installer.platform, 'archive_existing_installation', lambda *a, **kw: calls.append('archive'))
    installer._step_archive([])
    assert calls == ['stop', 'archive']
    calls.clear()
    def fail(*args):
        raise RuntimeError('still running')
    monkeypatch.setattr(installer.platform, 'quiesce_installation', fail)
    with pytest.raises(RuntimeError, match='still running'):
        installer._step_archive([])
    assert calls == []


def test_rollback_preserves_files_when_shutdown_unverified(tmp_path, monkeypatch):
    installer = make_installer(tmp_path)
    marker = tmp_path / 'credential-fixture'
    marker.write_text('synthetic')
    installer._tracked_files_created.append(marker)
    installer._tracked_services_started = ['chief-tls-proxy.service']
    def fail(*args):
        raise RuntimeError('still running')
    monkeypatch.setattr(installer.platform, 'quiesce_installation', fail, raising=False)
    installer._rollback_attempt()
    assert installer._cleanup_unverified
    assert marker.exists()


def test_start_intent_tracked_before_partial_enable_failure(tmp_path, monkeypatch):
    installer = make_installer(tmp_path)
    monkeypatch.setattr(installer.state_machine, 'transition', lambda *a, **kw: None)
    monkeypatch.setattr(installer, '_is_test_fixture', lambda: False)
    def fail(*args):
        raise RuntimeError('partial enable failure')
    monkeypatch.setattr(installer.platform, 'control_services', fail)
    with pytest.raises(RuntimeError):
        installer._step_health_check()
    assert set(installer._tracked_services_started) == {
        'chief-agent-dashboard.service', 'chief-agent-worker.service',
        'chief-agent-scheduler.service', 'chief-tls-proxy.service'}


@pytest.mark.parametrize('code,output', [(1, ''), (0, ''), (0, 'CHIEF_STOP_VERIFIED')])
def test_windows_cleanup_requires_positive_verification(tmp_path, monkeypatch, code, output):
    import deployment.installer.platforms.windows as module
    def run(command, **kwargs):
        script = base64.b64decode(command[-1]).decode('utf-16le')
        assert 'Disable-ScheduledTask' in script
        assert 'ExecutablePath.StartsWith($prefix' in script
        assert '-WindowStyle' in command and 'Hidden' in command
        return SimpleNamespace(returncode=code, stdout=output)
    monkeypatch.setattr(module, 'run_quiet', run)
    if code or not output:
        with pytest.raises(RuntimeError, match='preserving'):
            WindowsPlatformAdapter().quiesce_installation(tmp_path, [])
    else:
        WindowsPlatformAdapter().quiesce_installation(tmp_path, [])


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows process ownership validation')
def test_native_cleanup_kills_only_installation_process(tmp_path):
    root = tmp_path / 'owned'
    root.mkdir()
    original = Path(os.environ['SystemRoot']) / 'System32' / 'ping.exe'
    executable = root / 'ping.exe'
    shutil.copy2(original, executable)
    flags = subprocess.CREATE_NO_WINDOW
    owned = subprocess.Popen([str(executable), '-t', '127.0.0.1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    sentinel = subprocess.Popen([str(original), '-t', '127.0.0.1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    try:
        assert owned.poll() is None and sentinel.poll() is None
        WindowsPlatformAdapter().quiesce_installation(root, [])
        owned.wait(timeout=5)
        assert sentinel.poll() is None
        executable.rename(root / 'archived.exe')
    finally:
        for process in (owned, sentinel):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
