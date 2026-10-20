"""Tests for Chief Unified Installer shared contracts, state machine, and release manifest."""
import json
from pathlib import Path
import pytest

from deployment.installer import (
    ACCEPTED_FIRST_RELEASE_MANIFEST,
    EventBus,
    InstallEvent,
    InstallOptions,
    InstallerError,
    ManifestValidationError,
    ORDERED_INSTALL_STEPS,
    PlatformInfo,
    PreflightCheck,
    PreflightReport,
    ReleaseManifest,
    ReleaseMismatchError,
    STEP_DEFINITIONS,
    StateMachine,
    StepDefinition,
    StepId,
    StepStatus,
    parse_manifest,
)
from deployment.installer.platforms.base import PlatformAdapter


def test_accepted_first_release_manifest_validity():
    manifest = ACCEPTED_FIRST_RELEASE_MANIFEST
    assert manifest.release_id == "chief-2026-10-16-accepted"
    assert manifest.chief_agent_commit == "6ab701097b774fb3ea14513b164cdf05f74b7bf9"
    assert manifest.owner_pwa_commit == "b8d139b0789918f3c937e640cc618e66194c966b"
    assert manifest.staff_pwa_commit == "4a07d324e13630e2742a4e69127569d70a3cdf83"
    assert manifest.job_pwa_commit == "90eb0cfa2155c4d5f6a4ca8bf510a5d01e1f7db9"
    assert manifest.node_version == "22.23.3"
    assert manifest.caddy_version == "2.11.7"
    assert manifest.caddy_checksum == "5d27b76b95f496638d79208c55250317b8755fd9e90005f7466aa32956944a90"
    assert manifest.supported_installer_schema == 1


def test_accepted_first_release_manifest_is_immutable():
    """Ensure the historical accepted baseline is never silently mutated by review candidates."""
    historical = ACCEPTED_FIRST_RELEASE_MANIFEST
    assert historical.chief_agent_commit == "6ab701097b774fb3ea14513b164cdf05f74b7bf9"
    review_manifest_path = Path(__file__).resolve().parents[1] / "review-dns-manifest.json"
    if review_manifest_path.exists():
        review_manifest = parse_manifest(review_manifest_path)
        assert review_manifest.chief_agent_commit != historical.chief_agent_commit, (
            "review-dns-manifest.json must not silently mutate the historical accepted manifest"
        )


def test_manifest_component_verification():
    manifest = ACCEPTED_FIRST_RELEASE_MANIFEST
    # Exact match passes
    manifest.verify_component_commit("chief_agent", manifest.chief_agent_commit)
    manifest.verify_component_commit("owner_pwa", "b8d139b0789918f3c937e640cc618e66194c966b")
    manifest.verify_component_commit("staff_pwa", "4a07d324e13630e2742a4e69127569d70a3cdf83")
    manifest.verify_component_commit("job_pwa", "90eb0cfa2155c4d5f6a4ca8bf510a5d01e1f7db9")

    # Mismatch raises ReleaseMismatchError
    with pytest.raises(ReleaseMismatchError) as exc_info:
        manifest.verify_component_commit("chief_agent", "0000000000000000000000000000000000000000")
    assert "commit mismatch" in str(exc_info.value)

    # Unknown component raises ManifestValidationError
    with pytest.raises(ManifestValidationError):
        manifest.verify_component_commit("nonexistent_component", "819b7d76c9b17e0c58b50c32ea485991f163aeba")


def test_manifest_parse_valid_and_invalid(tmp_path):
    valid_dict = ACCEPTED_FIRST_RELEASE_MANIFEST.to_dict()
    parsed = parse_manifest(valid_dict)
    assert parsed == ACCEPTED_FIRST_RELEASE_MANIFEST

    # File path parsing
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(valid_dict), encoding="utf-8")
    assert parse_manifest(manifest_file) == ACCEPTED_FIRST_RELEASE_MANIFEST

    # Missing required field
    invalid_dict = valid_dict.copy()
    del invalid_dict["chief_agent_commit"]
    with pytest.raises(ManifestValidationError) as exc:
        parse_manifest(invalid_dict)
    assert "chief_agent_commit" in str(exc.value)

    # Unsupported schema version
    invalid_schema = valid_dict.copy()
    invalid_schema["supported_installer_schema"] = 2
    with pytest.raises(ManifestValidationError) as exc:
        parse_manifest(invalid_schema)
    assert "Unsupported installer schema" in str(exc.value)

    # Invalid commit SHA format (not 40 hex chars)
    invalid_sha = valid_dict.copy()
    invalid_sha["chief_agent_commit"] = "not-a-valid-sha"
    with pytest.raises(ManifestValidationError):
        parse_manifest(invalid_sha)

    # Invalid caddy checksum (not 64 hex chars)
    invalid_caddy_sha = valid_dict.copy()
    invalid_caddy_sha["caddy_checksum"] = "short-hash"
    with pytest.raises(ManifestValidationError):
        parse_manifest(invalid_caddy_sha)


def test_install_options_password_redaction():
    opts = InstallOptions(
        owner_name="Test Owner",
        owner_username="admin",
        owner_password="SuperSecretPassword123!",
        farm_operations="ENABLED",
        organization_name="Test Org",
        farm_name="Test Farm",
    )
    repr_str = repr(opts)
    str_str = str(opts)
    assert "SuperSecretPassword123!" not in repr_str
    assert "SuperSecretPassword123!" not in str_str
    assert "password_set=YES" in repr_str


def test_event_bus_listener_isolation():
    bus = EventBus()
    received = []

    def failing_listener(event):
        raise RuntimeError("Listener exploded")

    def working_listener(event):
        received.append(event)

    bus.subscribe(failing_listener)
    bus.subscribe(working_listener)

    event = bus.emit(StepId.PREFLIGHT, "Checking System", StepStatus.RUNNING, progress=0.1)
    assert len(received) == 1
    assert received[0].step_id == StepId.PREFLIGHT
    assert received[0].status == StepStatus.RUNNING

    bus.unsubscribe(working_listener)
    bus.emit(StepId.PREFLIGHT, "Checking System", StepStatus.SUCCESS, progress=1.0)
    assert len(received) == 1  # Unsubscribed listener did not receive second event


def test_state_machine_ordered_execution():
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    sm = StateMachine(event_bus=bus)

    assert sm.current_step == StepId.IDLE
    assert sm.current_status == StepStatus.PENDING

    # Step through all ordered installation steps
    for step in ORDERED_INSTALL_STEPS:
        sm.transition(step, StepStatus.RUNNING, message=f"Starting {step.value}")
        assert sm.current_step == step
        assert sm.current_status == StepStatus.RUNNING

        sm.transition(step, StepStatus.SUCCESS, message=f"Finished {step.value}", progress=1.0)
        assert sm.current_step == step
        assert sm.current_status == StepStatus.SUCCESS

    assert sm.current_step == StepId.COMPLETE
    assert len(events) == len(ORDERED_INSTALL_STEPS) * 2


def test_state_machine_invalid_transition_fails():
    sm = StateMachine()
    # Cannot jump directly from IDLE to PRODUCTION_INSTANCE
    with pytest.raises(InstallerError) as exc:
        sm.transition(StepId.PRODUCTION_INSTANCE, StepStatus.RUNNING)
    assert "Invalid installer transition" in str(exc.value)


def test_state_machine_failure_and_cancellation():
    # Failure from any state
    sm = StateMachine()
    sm.transition(StepId.PREFLIGHT, StepStatus.RUNNING)
    sm.fail(ValueError("Disk space depleted"), step_id=StepId.PREFLIGHT)
    assert sm.current_step == StepId.FAILED
    assert sm.current_status == StepStatus.FAILED
    assert "Disk space depleted" in sm.history[-1][2]

    # Cancellation from running state
    sm2 = StateMachine()
    sm2.transition(StepId.PREFLIGHT, StepStatus.RUNNING)
    sm2.cancel("User cancelled during preflight")
    assert sm2.current_step == StepId.CANCELLED
    assert sm2.is_cancelled


def test_step_definitions_completeness():
    for step_id in StepId:
        assert step_id in STEP_DEFINITIONS
        step_def = STEP_DEFINITIONS[step_id]
        assert isinstance(step_def, StepDefinition)
        assert step_def.step_id == step_id
        assert len(step_def.display_name) > 0


def test_platform_adapter_abc_enforcement():
    class IncompleteAdapter(PlatformAdapter):
        pass

    with pytest.raises(TypeError):
        IncompleteAdapter()


def test_default_candidate_matches_review_manifest_and_staff_runtime():
    from deployment.installer.manifest import DEFAULT_RELEASE_MANIFEST
    review=parse_manifest(Path(__file__).resolve().parents[1]/'review-dns-manifest.json')
    assert DEFAULT_RELEASE_MANIFEST==review
    assert 'review' in DEFAULT_RELEASE_MANIFEST.release_id
    assert DEFAULT_RELEASE_MANIFEST.node_version.split('.')[0]=='24'
    assert DEFAULT_RELEASE_MANIFEST.chief_agent_commit!=ACCEPTED_FIRST_RELEASE_MANIFEST.chief_agent_commit
