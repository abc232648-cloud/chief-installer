"""The packaged candidate and its review manifest must describe one release."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from deployment.installer.manifest import (
    ACCEPTED_FIRST_RELEASE_MANIFEST, CURRENT_RELEASE_MANIFEST, parse_manifest,
)
from deployment.installer.errors import InstallerError
from deployment.installer.orchestrator import InstallerOrchestrator, InstallerPaths
from deployment.installer.logging import InstallerLogger
from deployment.installer.models import InstallOptions, StepId, StepStatus
from deployment.installer.state import ORDERED_INSTALL_STEPS
from test_installer_orchestrator import FakePlatformAdapter


def test_current_packaged_manifest_matches_review_and_preserves_history():
    review = parse_manifest(Path(__file__).resolve().parents[1] / 'review-dns-manifest.json')
    assert CURRENT_RELEASE_MANIFEST == review
    assert review.node_version == '24.19.0'
    assert ACCEPTED_FIRST_RELEASE_MANIFEST.node_version == '22.23.3'
    assert review.chief_agent_commit != ACCEPTED_FIRST_RELEASE_MANIFEST.chief_agent_commit


def test_incompatible_client_engine_stops_before_build(tmp_path, monkeypatch):
    paths = InstallerPaths.create(custom_root=tmp_path/'chief', home=tmp_path/'home')
    runner = InstallerOrchestrator(
        platform=FakePlatformAdapter(), options=InstallOptions(owner_name='Fixture', owner_password='SyntheticPassword123!'),
        paths=paths, logger=InstallerLogger(log_dir=tmp_path/'logs', session_id='engine-check'),
    )
    monkeypatch.setattr(runner, '_is_test_fixture', lambda: False)
    monkeypatch.setattr(runner, '_acquire_node_runtime', lambda: (tmp_path/'node', tmp_path/'npm'))
    monkeypatch.setattr(runner, '_acquire_repository', lambda **kwargs: None)
    for step in ORDERED_INSTALL_STEPS:
        if step == StepId.PWA_BUILDS:
            break
        runner.state_machine.transition(step, StepStatus.SUCCESS)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1, stdout='', stderr='EBADENGINE')
    monkeypatch.setattr('deployment.installer.orchestrator.subprocess.run', run)
    with pytest.raises(InstallerError, match='npm ci failed'):
        runner._step_pwa_builds()
    assert len(calls) == 1
    assert calls[0][1:] == ['ci', '--engine-strict']
    assert not (paths.pwa_root/'owner'/'dist').exists()
