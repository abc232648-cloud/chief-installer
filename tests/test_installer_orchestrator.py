"""Tests for InstallerOrchestrator running against a fake platform adapter."""
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pytest

from deployment.installer.errors import PreflightError
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


class FakePlatformAdapter(PlatformAdapter):
    """Controllable test platform adapter."""

    def __init__(self, preflight_passes: bool = True, has_existing_install: bool = False):
        self.preflight_passes = preflight_passes
        self.has_existing_install = has_existing_install
        self.calls: List[str] = []

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
        return True

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
