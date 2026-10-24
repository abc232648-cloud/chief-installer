"""Tests for InstallerOrchestrator running against a fake platform adapter."""
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from deployment.installer.errors import InstallerError, PreflightError
from deployment.installer.events import EventBus
from deployment.installer.logging import InstallerLogger
from deployment.installer.manifest import ACCEPTED_FIRST_RELEASE_MANIFEST
from deployment.installer.models import (
    InstallOptions,
    PlatformInfo,
    PreflightCheck,
    PreflightReport,
    StepId,
    StepStatus,
)
from deployment.installer.orchestrator import InstallerOrchestrator, InstallerPaths
from deployment.installer.platforms.base import PlatformAdapter


@pytest.fixture(autouse=True)
def isolated_owner_home(tmp_path, monkeypatch):
    home = tmp_path / "owner-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


class FakePlatformAdapter(PlatformAdapter):
    """Controllable test platform adapter."""

    def __init__(self, preflight_passes: bool = True, has_existing_install: bool = False):
        self.preflight_passes = preflight_passes
        self.has_existing_install = has_existing_install
        self.calls: List[str] = []

    def get_current_username(self) -> str:
        self.calls.append("get_current_username")
        return "testuser"

    def get_platform_info(self) -> PlatformInfo:
        self.calls.append("get_platform_info")
        return PlatformInfo(
            os_name="linux",
            distro_id="xubuntu",
            distro_name="Xubuntu",
            distro_version="24.04",
            architecture="x86_64",
            is_supported=True,
        )

    def run_preflight_checks(self) -> PreflightReport:
        self.calls.append("run_preflight_checks")
        return PreflightReport(
            platform=self.get_platform_info(),
            checks=[
                PreflightCheck(name="os_supported", passed=self.preflight_passes, message="OS is Xubuntu"),
                PreflightCheck(name="disk_space", passed=self.preflight_passes, message="50GB available"),
            ],
            passed=self.preflight_passes,
        )

    def detect_existing_installation(self) -> Tuple[bool, List[str]]:
        self.calls.append("detect_existing_installation")
        if self.has_existing_install:
            return True, ["/home/user/chief-agent", "/home/user/.local/share/chief"]
        return False, []

    def archive_existing_installation(
        self,
        archive_root: Path,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Path:
        self.calls.append("archive_existing_installation")
        target = archive_root / "20261016_archive"
        target.mkdir(parents=True, exist_ok=True)
        if progress_callback:
            progress_callback(f"Archived to {target}")
        return target

    def install_system_dependencies(
        self,
        progress_callback: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        self.calls.append("install_system_dependencies")
        if progress_callback:
            progress_callback("All packages ready", 1.0)

    def enable_user_linger(self, username: Optional[str] = None) -> None:
        self.calls.append("enable_user_linger")

    def configure_local_discovery(
        self,
        owner_name: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        self.calls.append("configure_local_discovery")
        return "farm-owner.local"

    def set_executable_capability(self, executable: Path, capability: str) -> None:
        self.calls.append(f"setcap:{capability}")

    def install_service_units(self, stage_dir: Path, dest_dir: Path) -> None:
        self.calls.append("install_service_units")

    def reload_service_manager(self) -> None:
        self.calls.append("reload_service_manager")

    def control_services(self, action: str, service_names: List[str]) -> Dict[str, bool]:
        self.calls.append(f"control_services:{action}")
        return {s: True for s in service_names}

    def verify_service_active(self, service_name: str) -> bool:
        self.calls.append(f"verify_service_active:{service_name}")
        return getattr(self, "services_active", True)

    def export_device_ca(
        self,
        ca_cert_path: Path,
        destination_dir: Path,
        owner_name: str,
        public_host: str,
    ) -> Path:
        self.calls.append("export_device_ca")
        destination_dir.mkdir(parents=True, exist_ok=True)
        readme = destination_dir / "README.txt"
        readme.write_text(f"Device setup for {owner_name} at {public_host}", encoding="utf-8")
        return readme

    def open_url_in_browser(self, url: str) -> bool:
        self.calls.append(f"open_url:{url}")
        return True

    def install_wireguard_dependencies(self, progress_callback: Optional[Callable[[str], None]] = None) -> None:
        self.calls.append("install_wireguard_dependencies")

    def install_wireguard_system_config(self, source_conf: Path) -> None:
        self.calls.append("install_wireguard_system_config")

    def control_wireguard_service(self, action: str) -> bool:
        self.calls.append(f"control_wireguard_service:{action}")
        return getattr(self, "wireguard_service_success", True)

    def apply_wireguard_firewall(self, port: int) -> None:
        self.calls.append(f"apply_wireguard_firewall:{port}")
        if getattr(self, "fail_wireguard_firewall", False):
            from deployment.installer.errors import PrivilegeError
            raise PrivilegeError("Simulated firewall failure")

    def rollback_wireguard_firewall(self) -> None:
        self.calls.append("rollback_wireguard_firewall")

    def verify_wireguard_active(self) -> bool:
        self.calls.append("verify_wireguard_active")
        return getattr(self, "wireguard_active_result", True)

    def unregister_service_units(self, unit_files: List[Any]) -> None:
        self.calls.append("unregister_service_units")

    def terminate_service_processes(self, service_names: List[str]) -> None:
        self.calls.append("terminate_service_processes")

    def remove_console_launchers(self, paths: Any) -> List[str]:
        self.calls.append("remove_console_launchers")
        return []

    def cleanup_local_discovery(self, public_host: str) -> None:
        self.calls.append(f"cleanup_local_discovery:{public_host}")



def test_dependency_install_allows_silent_wheel_installation(tmp_path, monkeypatch):
    import subprocess
    paths = InstallerPaths.create(custom_root=tmp_path / "root", home=tmp_path / "home")
    paths.tooling.mkdir(parents=True)
    (paths.tooling / "requirements.txt").write_text("example==1.0\n")
    instance = InstallerOrchestrator(
        platform=FakePlatformAdapter(), paths=paths,
        options=InstallOptions(owner_name="Test", owner_password="Synthetic-only"),
        logger=InstallerLogger(log_dir=tmp_path / "logs"),
    )
    calls = []
    def command(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(instance, "run_command_streaming", command)
    monkeypatch.setattr(instance, "_is_test_fixture", lambda: False)
    instance.state_machine.current_step = StepId.SYSTEM_DEPENDENCIES
    instance._step_runtime()
    install = next(kw for args, kw in calls if "-r" in args)
    assert install.get("inactivity_timeout") is None
    assert 0 < install["timeout"] <= 900


def test_orchestrator_clean_host_install(tmp_path):
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_clean_install")
    event_bus = EventBus()
    events = []
    event_bus.subscribe(events.append)

    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    options = InstallOptions(
        owner_name="Acme Farms",
        owner_username="admin",
        owner_password="SuperSecretPassword!",
        farm_operations="ENABLED",
        organization_name="Acme Org",
        farm_name="Main Farm",
        allow_test_mock=True,
    )

    orchestrator = InstallerOrchestrator(
        platform=platform,
        options=options,
        manifest=ACCEPTED_FIRST_RELEASE_MANIFEST,
        paths=paths,
        event_bus=event_bus,
        logger=logger,
    )

    result = orchestrator.execute_all()

    assert result.status == "SUCCESS"
    assert result.public_host == "farm-owner.local"
    assert "https://farm-owner.local/" in result.urls["chief"]
    assert "https://farm-owner.local/owner/" in result.urls["owner"]
    assert "https://farm-owner.local/staff/" in result.urls["staff"]
    assert "https://farm-owner.local/jobs/" in result.urls["jobs"]
    assert orchestrator.current_step == StepId.COMPLETE

    # Verify calls
    assert "run_preflight_checks" in platform.calls
    assert "detect_existing_installation" in platform.calls
    # Archive should NOT be called on clean install
    assert "archive_existing_installation" not in platform.calls
    assert "install_system_dependencies" in platform.calls
    assert "configure_local_discovery" in platform.calls
    assert "export_device_ca" in platform.calls

    # Security check: password must NEVER be in log file
    log_content = logger.log_file.read_text(encoding="utf-8")
    assert "SuperSecretPassword!" not in log_content


def test_orchestrator_existing_install_archive(tmp_path):
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_archive")
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=True)
    options = InstallOptions(owner_name="Test Owner", owner_password="SecretPassword123", allow_test_mock=True)

    orchestrator = InstallerOrchestrator(
        platform=platform,
        options=options,
        paths=paths,
        logger=logger,
    )

    result = orchestrator.execute_all()
    assert result.status == "SUCCESS"
    assert "archive_existing_installation" in platform.calls
    assert result.archive_path is not None


def test_orchestrator_release_validation_fails_closed_when_contract_missing(tmp_path):
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_fail_closed")
    platform = FakePlatformAdapter(preflight_passes=True)
    options = InstallOptions(owner_name="Test Owner", owner_password="SecretPassword123", allow_test_mock=False)

    orchestrator = InstallerOrchestrator(
        platform=platform,
        options=options,
        paths=paths,
        logger=logger,
    )

    result = orchestrator.execute_all()
    assert result.status == "FAILED"
    assert orchestrator.state_machine.current_step == StepId.FAILED


def test_orchestrator_preflight_failure(tmp_path):
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_fail")
    platform = FakePlatformAdapter(preflight_passes=False)
    options = InstallOptions(owner_name="Test Owner", owner_password="SecretPassword123")

    orchestrator = InstallerOrchestrator(
        platform=platform,
        options=options,
        paths=paths,
        logger=logger,
    )

    # Calling run_preflight directly raises PreflightError
    with pytest.raises(PreflightError):
        orchestrator.run_preflight()

    # Calling execute_all catches and returns FAILED result
    result = orchestrator.execute_all()
    assert result.status == "FAILED"
    assert result.error_code == "PREFLIGHT_FAILED"


def test_orchestrator_cancellation(tmp_path):
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_cancel")
    platform = FakePlatformAdapter(preflight_passes=True)
    options = InstallOptions(owner_name="Test Owner", owner_password="SecretPassword123")

    orchestrator = InstallerOrchestrator(
        platform=platform,
        options=options,
        paths=paths,
        logger=logger,
    )

    # Cancel immediately
    orchestrator.cancel()
    result = orchestrator.execute_all()
    assert result.status == "CANCELLED"
    assert orchestrator.current_step == StepId.CANCELLED


def test_orchestrator_diagnostic_report_no_secrets(tmp_path):
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_diag")
    logger.info("Setting up user with password='SuperSecretPassword123' and token=abcde12345")
    report = logger.generate_diagnostic_report(failed_step="RUNTIME", error=ValueError("Failed with password='SuperSecretPassword123'"))

    assert "SuperSecretPassword123" not in report
    assert "[REDACTED]" in report


def test_step_browser_fails_closed_when_version_check_fails(tmp_path, monkeypatch):
    """Test that _step_browser fails closed instead of falling back to a synthesized version."""
    import subprocess
    monkeypatch.setattr('deployment.installer.desktop_runtime.provision', lambda *a, **kw: None)
    from deployment.installer.errors import InstallerError

    class BrowserTestPlatform(FakePlatformAdapter):
        pass

    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_browser_fail")
    platform = BrowserTestPlatform()
    options = InstallOptions(owner_name="Test Owner", owner_password="SecretPassword123", allow_test_mock=False)

    orchestrator = InstallerOrchestrator(
        platform=platform,
        options=options,
        paths=paths,
        logger=logger,
    )
    orchestrator.state_machine.current_step = StepId.RUNTIME

    # Mock subprocess.run
    def fake_subprocess_run(cmd, *args, **kwargs):
        cmd_str = " ".join(str(c) for c in cmd)
        if "playwright install" in cmd_str:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        elif "-c" in cmd and "executable_path" in kwargs.get("input", "") or "-c" in cmd and "executable_path" in str(cmd):
            fake_bin = tmp_path / "fake_chromium"
            fake_bin.write_bytes(b"mock")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=str(fake_bin), stderr="")
        elif "sync_playwright" in str(cmd):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="CHIEF_PLAYWRIGHT_BROWSER_OK", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr("os.access", lambda path, mode: True)

    with pytest.raises(InstallerError, match="Headless Chromium check did not report a valid version"):
        orchestrator._step_browser()


def test_step_browser_fails_closed_when_smoke_test_fails(tmp_path, monkeypatch):
    """Test that _step_browser fails closed when the Playwright smoke launch test fails."""
    import subprocess
    monkeypatch.setattr('deployment.installer.desktop_runtime.provision', lambda *a, **kw: None)
    from deployment.installer.errors import InstallerError

    class BrowserTestPlatform(FakePlatformAdapter):
        pass

    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_smoke_fail")
    platform = BrowserTestPlatform()
    options = InstallOptions(owner_name="Test Owner", owner_password="SecretPassword123", allow_test_mock=False)

    orchestrator = InstallerOrchestrator(
        platform=platform,
        options=options,
        paths=paths,
        logger=logger,
    )
    orchestrator.state_machine.current_step = StepId.RUNTIME

    fake_bin = tmp_path / "fake_chromium"
    fake_bin.write_bytes(b"mock")

    def fake_subprocess_run(cmd, *args, **kwargs):
        cmd_str = " ".join(str(c) for c in cmd)
        if "playwright install" in cmd_str:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        elif "-c" in cmd and "executable_path" in str(cmd):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=str(fake_bin), stderr="")
        elif "--version" in cmd_str:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="Chromium 154.0.8037.57", stderr="")
        elif "-c" in cmd and "CHIEF_PLAYWRIGHT_BROWSER_OK" in str(cmd):
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="Browser launch failed: missing shared library")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr("os.access", lambda path, mode: True)

    with pytest.raises(InstallerError, match="Chromium browser launch smoke test failed"):
        orchestrator._step_browser()


def test_orchestrator_https_failure_diagnostics_sanitized(tmp_path, monkeypatch):
    """Verify HTTPS verification failure captures route, status/error, CA path without leaking secrets."""
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_https_diag")
    platform = FakePlatformAdapter()
    options = InstallOptions(
        owner_name="Test Owner",
        owner_password="SuperSecretPassword!",
        tls_mode="LOCAL",
        allow_test_mock=True,
    )
    orchestrator = InstallerOrchestrator(
        platform=platform,
        options=options,
        paths=paths,
        logger=logger,
    )
    orchestrator._public_host = "test-host.local"

    # Advance clock past deadline and skip sleeps
    clock = [1000.0]
    def fake_monotonic():
        val = clock[0]
        clock[0] += 10.0
        return val
    monkeypatch.setattr("time.monotonic", fake_monotonic)
    monkeypatch.setattr("time.sleep", lambda s: None)

    # In LOCAL mode, when root.crt does not exist, _verify_https_routes raises FileNotFoundError and includes CA path
    with pytest.raises(InstallerError) as exc_info:
        orchestrator._verify_https_routes()

    err_text = str(exc_info.value)
    assert "Trusted HTTPS route verification failed on route '/login'" in err_text
    assert "https://test-host.local/login" in err_text
    assert "Expected root CA:" in err_text
    assert "root.crt" in err_text
    assert "(exists: False)" in err_text
    assert "SuperSecretPassword" not in err_text


def test_orchestrator_failure_rollback_cleans_credentials_preserves_logs(tmp_path, monkeypatch):
    """Verify rollback on failure stops/disables services, cleans credentials, and preserves diagnostic log."""
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_rollback")
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    options = InstallOptions(
        owner_name="Rollback Farm",
        owner_username="admin",
        owner_password="SecretPassword123!",
        farm_operations="ENABLED",
        organization_name="Acme Org",
        farm_name="Main Farm",
        allow_test_mock=True,
    )

    orchestrator = InstallerOrchestrator(
        platform=platform,
        options=options,
        manifest=ACCEPTED_FIRST_RELEASE_MANIFEST,
        paths=paths,
        logger=logger,
    )

    # Inject failure at health check step
    def failing_health_check():
        # Start services and create files first
        orchestrator._tracked_services_started = [
            "chief-agent-dashboard.service", "chief-agent-worker.service",
            "chief-agent-scheduler.service", "chief-tls-proxy.service"
        ]
        raise InstallerError("Simulated HTTPS readiness failure")

    monkeypatch.setattr(orchestrator, "_step_health_check", failing_health_check)

    result = orchestrator.execute_all()

    assert result.status == "FAILED"
    assert "Simulated HTTPS readiness failure" in result.error

    # Verify rollback called stop and disable
    assert "control_services:stop" in platform.calls
    assert "control_services:disable" in platform.calls

    # Verify partial credentials and configs are removed
    assert not paths.identity_config.exists()
    assert not paths.config.exists()

    # Verify diagnostic log file is preserved!
    assert Path(result.log_path).exists()
    assert Path(result.log_path).stat().st_size > 0


def test_orchestrator_cancellation_rollback_cleans_credentials(tmp_path, monkeypatch):
    """Verify user cancellation triggers rollback and leaves no credentials behind."""
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_cancel_rollback")
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    options = InstallOptions(
        owner_name="Cancel Farm",
        owner_username="admin",
        owner_password="SecretPassword123!",
        allow_test_mock=True,
    )

    orchestrator = InstallerOrchestrator(
        platform=platform,
        options=options,
        manifest=ACCEPTED_FIRST_RELEASE_MANIFEST,
        paths=paths,
        logger=logger,
    )

    # Cancel right after identity step
    original_step_identity = orchestrator._step_identity
    def cancelling_identity():
        original_step_identity()
        assert paths.identity_config.exists()
        orchestrator.cancel()

    monkeypatch.setattr(orchestrator, "_step_identity", cancelling_identity)

    result = orchestrator.execute_all()

    assert result.status == "CANCELLED"
    # Credentials must be cleaned up by rollback
    assert not paths.identity_config.exists()
    # Log must exist
    assert Path(result.log_path).exists()


def test_orchestrator_successful_install_preserves_services_and_files(tmp_path):
    """Verify successful install preserves services and does not roll back."""
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_success_preserves")
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    options = InstallOptions(
        owner_name="Success Farm",
        owner_username="admin",
        owner_password="SecretPassword123!",
        farm_operations="ENABLED",
        organization_name="Acme Org",
        farm_name="Main Farm",
        allow_test_mock=True,
    )

    orchestrator = InstallerOrchestrator(
        platform=platform,
        options=options,
        manifest=ACCEPTED_FIRST_RELEASE_MANIFEST,
        paths=paths,
        logger=logger,
    )

    result = orchestrator.execute_all()

    assert result.status == "SUCCESS"
    # Rollback must NOT have been called
    assert "unregister_service_units" not in platform.calls
    # Services were enabled and started
    assert "control_services:start" in platform.calls
    # Files remain intact
    assert paths.identity_config.exists()
    assert paths.config.exists()


def test_orchestrator_full_purge(tmp_path):
    """Verify uninstall_chief with purge=True cleans all Chief paths while preserving unrelated files."""
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_purge")
    platform = FakePlatformAdapter()
    platform.services_active = False

    # Create dummy Chief files
    paths.identity_config.parent.mkdir(parents=True, exist_ok=True)
    paths.identity_config.write_text('{"owner": "admin"}', encoding="utf-8")
    paths.config.write_text('{"db": "production"}', encoding="utf-8")
    paths.caddyfile.write_text('localhost { respond "ok" }', encoding="utf-8")
    paths.production.mkdir(parents=True, exist_ok=True)
    (paths.production / "chief.sqlite").write_text("database content", encoding="utf-8")
    paths.archive_base.mkdir(parents=True, exist_ok=True)
    (paths.archive_base / "old_backup.tar.gz").write_text("backup content", encoding="utf-8")

    # Create unrelated files
    unrelated_dir = tmp_path / "unrelated_user_dir"
    unrelated_dir.mkdir(parents=True, exist_ok=True)
    unrelated_file = unrelated_dir / "crop_data.csv"
    unrelated_file.write_text("corn,wheat,soy", encoding="utf-8")

    import pytest
    from deployment.installer.errors import InstallerError
    from deployment.installer.orchestrator import uninstall_chief

    with pytest.raises(InstallerError):
        uninstall_chief(paths=paths, platform=platform, purge=True, confirmed=False, logger=logger, service_stop_timeout=0.1)

    report = uninstall_chief(paths=paths, platform=platform, purge=True, confirmed=True, logger=logger, service_stop_timeout=0.1)

    assert report["success"] is True
    assert "chief-agent-dashboard.service" in report["stopped_services"]

    # Chief files must be removed
    assert not paths.identity_config.exists()
    assert not paths.config.exists()
    assert not paths.caddyfile.exists()
    assert not paths.production.exists()
    assert not paths.archive_base.exists()

    # Unrelated files must be preserved!
    assert unrelated_file.exists()
    assert unrelated_file.read_text(encoding="utf-8") == "corn,wheat,soy"


