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
