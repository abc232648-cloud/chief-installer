"""Regression tests for the installer health-check wall-clock deadline."""
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pytest

import deployment.installer.orchestrator as orchestrator_module
from deployment.installer.errors import InstallerError
from deployment.installer.logging import InstallerLogger
from deployment.installer.models import InstallOptions, PlatformInfo, PreflightReport, StepId, StepStatus
from deployment.installer.orchestrator import InstallerOrchestrator, InstallerPaths
from deployment.installer.platforms.base import PlatformAdapter


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class _DeploymentClient:
    def start_linux_services(self, config: Path) -> None:
        return None


class _DeadlinePlatform(PlatformAdapter):
    """Non-mock platform whose failed probes consume the supplied timeout."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.probe_timeouts: List[float] = []

    def get_platform_info(self) -> PlatformInfo:
        return PlatformInfo(
            os_name="linux",
            distro_id="xubuntu",
            distro_name="Xubuntu",
            distro_version="24.04",
            architecture="x86_64",
            is_supported=True,
        )

    def get_current_username(self) -> str:
        return "testuser"

    def run_preflight_checks(self) -> PreflightReport:
        raise NotImplementedError

    def detect_existing_installation(self) -> Tuple[bool, List[str]]:
        return False, []

    def archive_existing_installation(
        self,
        archive_root: Path,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Path:
        return archive_root

    def install_system_dependencies(
        self,
        progress_callback: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        return None

    def enable_user_linger(self, username: Optional[str] = None) -> None:
        return None

    def configure_local_discovery(
        self,
        owner_name: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        return "chief-test.local"

    def set_executable_capability(self, executable: Path, capability: str) -> None:
        return None

    def install_service_units(self, stage_dir: Path, dest_dir: Path) -> None:
        return None

    def reload_service_manager(self) -> None:
        return None

    def control_services(self, action: str, service_names: List[str]) -> Dict[str, bool]:
        return {name: True for name in service_names}

    def verify_service_active(self, service_name: str) -> bool:
        return True

    def export_device_ca(
        self,
        ca_cert_path: Path,
        destination_dir: Path,
        owner_name: str,
        public_host: str,
    ) -> Path:
        return destination_dir / "README.txt"

    def open_url_in_browser(self, url: str) -> bool:
        return True

    def probe_health_endpoint(self, url: str, timeout: float = 2.0) -> bool:
        self.probe_timeouts.append(timeout)
        self.clock.now += timeout
        return False


def test_health_check_enforces_15_second_wall_clock_deadline(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(orchestrator_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(orchestrator_module.time, "sleep", clock.sleep)

    paths = InstallerPaths.create(custom_root=tmp_path / "chief", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="health_deadline")
    platform = _DeadlinePlatform(clock)
    options = InstallOptions(
        owner_name="Deadline Test",
        owner_password="StrongPassword2026!",
        allow_test_mock=False,
    )
    orchestrator = InstallerOrchestrator(
        platform=platform,
        options=options,
        paths=paths,
        logger=logger,
        deployment_client=_DeploymentClient(),
    )

    with pytest.raises(InstallerError, match="15-second deadline"):
        orchestrator._step_health_check()

    assert clock.now == pytest.approx(15.0)
    assert platform.probe_timeouts
    assert all(timeout > 0 for timeout in platform.probe_timeouts)
    assert all(timeout <= 2.0 for timeout in platform.probe_timeouts)
    assert orchestrator.state_machine.current_step == StepId.HEALTH_CHECK
    assert orchestrator.state_machine.current_status == StepStatus.RUNNING
