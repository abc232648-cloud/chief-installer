import json
import os
from pathlib import Path
import stat
import zipfile

import pytest

from deployment.installer import desktop_runtime as desktop
from deployment.installer.orchestrator import InstallerPaths


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/escape', 'dir\\escape', 'a/../../b'])
def test_engine_archive_rejects_escape_before_creating_destination(tmp_path, name):
    archive = tmp_path / 'engine.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        entry = zipfile.ZipInfo('placeholder')
        entry.filename = name
        bundle.writestr(entry, 'bad')
    with pytest.raises(ValueError):
        desktop.extract(archive, tmp_path / 'new')
    assert not (tmp_path / 'new').exists()


def test_engine_archive_rejects_symbolic_link_and_case_collision(tmp_path):
    for kind in ('symlink', 'collision'):
        archive = tmp_path / (kind + '.zip')
        with zipfile.ZipFile(archive, 'w') as bundle:
            if kind == 'symlink':
                entry = zipfile.ZipInfo('link')
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                bundle.writestr(entry, '/outside')
            else:
                bundle.writestr('Engine', 'one'); bundle.writestr('engine', 'two')
        with pytest.raises(ValueError):
            desktop.extract(archive, tmp_path / kind)


def fixture_install(tmp_path, monkeypatch):
    paths = InstallerPaths.create(custom_root=tmp_path / 'chief', home=tmp_path / 'home')
    archive = tmp_path / 'fixture.zip'
    executable = 'electron.exe' if os.name == 'nt' else 'electron'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr(executable, b'synthetic-runtime')
    key = desktop.sys.platform + '-x64'
    # This is an archive/provision unit fixture, not an administrator sandbox test.
    monkeypatch.setattr(desktop, 'protected_linux_runtime', lambda archive: paths.root / 'desktop' / f'electron-{desktop.VERSION}-{key}')
    monkeypatch.setitem(desktop.PINS, key, desktop.digest(archive))
    helper = tmp_path / 'installer'
    helper.write_bytes(b'synthetic-helper')
    calls = []
    def download(url, destination, **kwargs):
        calls.append(url)
        destination.write_bytes(archive.read_bytes())
    record = desktop.provision(paths, download, helper=helper)
    return paths, record, calls


def test_provision_checks_release_hash_and_preserves_separate_profiles(tmp_path, monkeypatch):
    paths, record, calls = fixture_install(tmp_path, monkeypatch)
    assert len(calls) == 1 and calls[0].startswith('https://github.com/electron/electron/')
    assert desktop.installed(paths) == record
    assert not paths.browser_config.exists()
    paths.config_root.mkdir(parents=True, exist_ok=True)
    paths.config.write_text(json.dumps({'tls_mode': 'LOCAL'}))
    command = desktop.launch_command(paths, 'chief.local')
    assert command[-2] == '--chief-config'
    config = json.loads(Path(command[-1]).read_text())
    assert config['origin'] == 'https://chief.local'
    assert config['helper'] == record['helper']
    assert not any('password' in key or 'token' in key for key in config)
    assert '--app=' not in ' '.join(command)


@pytest.mark.parametrize('component', ['executable', 'helper', 'application'])
def test_modified_shell_components_are_rejected(tmp_path, monkeypatch, component):
    paths, record, _ = fixture_install(tmp_path, monkeypatch)
    target = Path(record[component])
    if component == 'application':
        target /= 'main.cjs'
    target.write_bytes(b'changed')
    with pytest.raises(ValueError):
        desktop.installed(paths)


def test_mismatched_engine_download_never_activates(tmp_path, monkeypatch):
    paths = InstallerPaths.create(custom_root=tmp_path / 'chief', home=tmp_path / 'home')
    with pytest.raises(ValueError, match='checksum'):
        desktop.provision(paths, lambda url, destination, **kw: destination.write_bytes(b'wrong'))
    assert not (paths.root / 'desktop/current.json').exists()


def test_service_control_rejects_uninstalled_root_without_touching_services(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr('deployment.installer.cli.get_default_platform_adapter', lambda: pytest.fail('No service action permitted'))
    assert desktop.control_main(['stop', '--root', str(tmp_path / 'chief'), '--home', str(tmp_path / 'home')]) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'SETUP_REQUIRED'

def test_stop_requires_each_writer_to_acknowledge_its_exact_run(tmp_path, monkeypatch):
    paths = InstallerPaths.create(custom_root=tmp_path / 'chief', home=tmp_path / 'home')
    folder = paths.production / 'service-control'
    folder.mkdir(parents=True)
    token = 'a' * 32
    status = folder / 'dashboard.json'
    status.write_text(json.dumps({'state': 'READY', 'run_id': token}))
    class Adapter:
        def verify_service_active(self, name): return name.endswith('dashboard.service')
        def quiesce_installation(self, *args): pytest.fail('Cannot stop proxy before writer acknowledgement')
        def control_services(self, *args): pytest.fail('Cannot stop proxy before writer acknowledgement')
    assert desktop.stop_services(paths, Adapter(), timeout=0) is False
    assert json.loads((folder / 'dashboard.stop.json').read_text()) == {'run_id': token}


def test_stop_rejects_restarted_writer(tmp_path, monkeypatch):
    paths = InstallerPaths.create(custom_root=tmp_path / 'chief', home=tmp_path / 'home')
    folder = paths.production / 'service-control'; folder.mkdir(parents=True)
    status = folder / 'worker.json'
    status.write_text(json.dumps({'state': 'READY', 'run_id': 'a' * 32}))
    original = desktop.write_json
    def restart(path, value):
        original(path, value)
        status.write_text(json.dumps({'state': 'STOPPED', 'run_id': 'b' * 32}))
    monkeypatch.setattr(desktop, 'write_json', restart)
    class Adapter:
        def verify_service_active(self, name): return False
        def quiesce_installation(self, *args): pytest.fail('Restart was not acknowledged')
    assert desktop.stop_services(paths, Adapter()) is False

def test_reprovision_does_not_bless_modified_runtime(tmp_path, monkeypatch):
    paths, record, _ = fixture_install(tmp_path, monkeypatch)
    Path(record['executable']).write_bytes(b'tampered-runtime')
    with pytest.raises(ValueError, match='modified'):
        desktop.provision(paths, lambda *a, **k: pytest.fail('Archive is already verified'), helper=record['helper'])

def test_privileged_engine_install_requires_root(tmp_path, monkeypatch):
    from deployment.installer.privileged import linux
    monkeypatch.setattr(linux.os, 'geteuid', lambda: 1000, raising=False)
    assert linux.run_install_desktop_engine(str(tmp_path / 'untrusted.zip')) == 2


def test_privileged_engine_command_rejects_extra_target_arguments():
    from deployment.installer.privileged import linux
    assert linux.main(['helper', 'install-desktop-engine', 'engine.zip', '/arbitrary/target']) == 2
