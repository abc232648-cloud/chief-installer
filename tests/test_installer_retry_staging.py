"""Retry validation must reconcile existing checkouts before the handshake."""
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest
from deployment.installer.orchestrator import InstallerOrchestrator, InstallerPaths
from deployment.installer.models import InstallOptions
from deployment.installer.logging import InstallerLogger
from deployment.installer.errors import InstallerError
from tests.test_installer_orchestrator import FakePlatformAdapter


def make_installer(tmp_path):
    return InstallerOrchestrator(
        platform=FakePlatformAdapter(),
        options=InstallOptions(owner_name='Test',owner_password='SyntheticPassword123'),
        paths=InstallerPaths.create(custom_root=tmp_path/'chief',home=tmp_path/'home'),
        logger=InstallerLogger(log_dir=tmp_path/'logs',session_id='retry'),
    )


def test_existing_staging_is_reconciled_before_contract_check(tmp_path,monkeypatch):
    installer=make_installer(tmp_path)
    for root in (installer.paths.tooling,installer.paths.release_source):
        (root/'deployment').mkdir(parents=True)
        (root/'deployment/contract.py').write_text('# stale checkout')
    monkeypatch.setattr(installer,'_is_test_fixture',lambda:False)
    calls=[]
    monkeypatch.setattr(installer,'_acquire_repository',lambda **kw:calls.append(kw))
    def verify(root,**kwargs):
        assert [call['target_dir'] for call in calls]==[installer.paths.tooling,installer.paths.release_source]
        assert all(call['commit']==installer.manifest.chief_agent_commit for call in calls)
        assert kwargs['expected_commit']==installer.manifest.chief_agent_commit
        return SimpleNamespace(chief_agent_commit=installer.manifest.chief_agent_commit)
    monkeypatch.setattr(installer.deployment_client,'verify_compatibility',verify)
    installer._step_preflight()
    installer._step_release_validation()


def test_retry_preserves_local_changes(tmp_path):
    installer=make_installer(tmp_path)
    root=tmp_path/'checkout'
    subprocess.run(['git','init',str(root)],check=True,capture_output=True)
    (root/'local-note.txt').write_text('must remain unchanged')
    with pytest.raises(InstallerError,match='local changes'):
        installer._acquire_repository(root,'unused',installer.manifest.chief_agent_commit,'chief_agent')
    assert (root/'local-note.txt').read_text()=='must remain unchanged'


def test_retry_updates_clean_staging_to_exact_pin(tmp_path, monkeypatch):
    from dataclasses import replace
    source = tmp_path / 'source'
    def git(*args):
        return subprocess.run(['git', *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()
    git('init', source)
    (source / 'version').write_text('old')
    git('-C', source, 'add', 'version')
    git('-C', source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'old')
    installer = make_installer(tmp_path)
    for target in (installer.paths.tooling, installer.paths.release_source):
        target.parent.mkdir(parents=True, exist_ok=True)
        git('clone', source, target)
    (source / 'version').write_text('pinned')
    git('-C', source, 'add', 'version')
    git('-C', source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'pinned')
    commit = git('-C', source, 'rev-parse', 'HEAD')
    installer.manifest = replace(installer.manifest, chief_agent_commit=commit)
    monkeypatch.setenv('CHIEF_AGENT_SOURCE', str(source))
    for target in (installer.paths.tooling, installer.paths.release_source):
        installer._acquire_repository(target, str(source), commit, 'chief_agent')
        assert git('-C', target, 'rev-parse', 'HEAD') == commit
        assert (target / 'version').read_text() == 'pinned'


def test_retry_replaces_non_git_partial_pwa_staging_and_preserves_it(tmp_path):
    from dataclasses import replace

    source = tmp_path / 'pwa-source'
    source.mkdir()
    def git(*args):
        return subprocess.run(['git', *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()
    git('init', source)
    (source / 'package.json').write_text('{"name":"jobs"}', encoding='utf-8')
    (source / 'package-lock.json').write_text('{"name":"jobs","lockfileVersion":3,"packages":{}}', encoding='utf-8')
    git('-C', source, 'add', 'package.json', 'package-lock.json')
    git('-C', source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'pinned PWA')
    commit = git('-C', source, 'rev-parse', 'HEAD')

    installer = make_installer(tmp_path)
    installer.manifest = replace(installer.manifest, job_pwa_commit=commit)
    target = tmp_path / 'chief' / 'pwas' / 'jobs'
    target.mkdir(parents=True)
    (target / 'partial-download.txt').write_text('preserve me', encoding='utf-8')

    installer._acquire_repository(target, str(source), commit, 'job_pwa')

    actual = subprocess.run(['git', '-C', str(target), 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
    assert actual == commit
    installer._verify_pwa_install_inputs(target, 'jobs')
    preserved = list(target.parent.glob('jobs.preserved-*'))
    assert len(preserved) == 1
    assert (preserved[0] / 'partial-download.txt').read_text(encoding='utf-8') == 'preserve me'


def test_failed_reclone_restores_non_git_partial_staging(tmp_path):
    installer = make_installer(tmp_path)
    target = tmp_path / 'chief' / 'pwas' / 'jobs'
    target.mkdir(parents=True)
    note = target / 'partial-download.txt'
    note.write_text('preserve me', encoding='utf-8')

    with pytest.raises(InstallerError, match='Failed to clone job_pwa'):
        installer._acquire_repository(
            target,
            str(tmp_path / 'missing-repository'),
            installer.manifest.job_pwa_commit,
            'job_pwa',
        )

    assert note.read_text(encoding='utf-8') == 'preserve me'
    assert not list(target.parent.glob('jobs.preserved-*'))


def test_pwa_install_inputs_reject_missing_or_invalid_lockfile(tmp_path):
    app_dir = tmp_path / 'jobs'
    app_dir.mkdir()
    (app_dir / 'package.json').write_text('{"name":"jobs"}', encoding='utf-8')
    with pytest.raises(InstallerError, match='package-lock.json or npm-shrinkwrap.json'):
        InstallerOrchestrator._verify_pwa_install_inputs(app_dir, 'jobs')

    (app_dir / 'package-lock.json').write_text('{"lockfileVersion":0}', encoding='utf-8')
    with pytest.raises(InstallerError, match='supported lockfileVersion'):
        InstallerOrchestrator._verify_pwa_install_inputs(app_dir, 'jobs')

    (app_dir / 'package-lock.json').write_text('{not valid json', encoding='utf-8')
    with pytest.raises(InstallerError, match='unreadable or invalid'):
        InstallerOrchestrator._verify_pwa_install_inputs(app_dir, 'jobs')


def test_cached_wrong_node_version_is_not_reused(tmp_path, monkeypatch):
    import os
    import deployment.installer.orchestrator as module
    installer = make_installer(tmp_path)
    windows = os.name == 'nt'
    cached = installer.paths.node_root / ('' if windows else 'bin')
    cached.mkdir(parents=True)
    (cached / ('node.exe' if windows else 'node')).touch()
    (cached / ('npm.cmd' if windows else 'npm')).touch()
    system_node = tmp_path / 'system-node'
    system_npm = tmp_path / 'system-npm'
    monkeypatch.setattr(module.shutil, 'which', lambda name: str(system_node if name == 'node' else system_npm))
    def version(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout='v' + (installer.manifest.node_version if command[0] == str(system_node) else '22.23.3'))
    monkeypatch.setattr(module, 'run_quiet', version)
    assert installer._acquire_node_runtime() == (system_node, system_npm)
