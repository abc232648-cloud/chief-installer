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

    # Isolate the health step while preserving its valid predecessor state.
    orchestrator.state_machine.current_step = StepId.LOCAL_DISCOVERY
    orchestrator.state_machine.current_status = StepStatus.SUCCESS

    with pytest.raises(InstallerError, match="15-second deadline"):
        orchestrator._step_health_check()

    assert clock.now == pytest.approx(15.0)
    assert platform.probe_timeouts
    assert all(timeout > 0 for timeout in platform.probe_timeouts)
    assert all(timeout <= 2.0 for timeout in platform.probe_timeouts)
    assert orchestrator.state_machine.current_step == StepId.HEALTH_CHECK
    assert orchestrator.state_machine.current_status == StepStatus.RUNNING


@pytest.mark.parametrize("missing_result", [False, True])
def test_automatic_startup_failure_blocks_completion(tmp_path, monkeypatch, missing_result):
    platform = _DeadlinePlatform(_Clock())
    calls = []
    def control(action, names):
        calls.append(action)
        return {} if missing_result else {name: False for name in names}
    monkeypatch.setattr(platform, "control_services", control)
    orchestrator = InstallerOrchestrator(
        platform=platform,
        options=InstallOptions(owner_name="Test", owner_password="StrongPassword2026!"),
        paths=InstallerPaths.create(custom_root=tmp_path / "chief", home=tmp_path / "home"),
        logger=InstallerLogger(log_dir=tmp_path / "logs", session_id="startup"),
        deployment_client=_DeploymentClient(),
    )
    orchestrator.state_machine.current_step = StepId.LOCAL_DISCOVERY
    orchestrator.state_machine.current_status = StepStatus.SUCCESS
    with pytest.raises(InstallerError, match="automatic startup"):
        orchestrator._step_health_check()
    assert calls == ["enable"]
    assert not platform.probe_timeouts
    assert orchestrator.state_machine.current_status != StepStatus.SUCCESS


@pytest.mark.parametrize('startup_delay', [10, 999])
def test_service_startup_can_be_slow_but_remains_bounded(tmp_path, monkeypatch, startup_delay):
    clock = _Clock()
    monkeypatch.setattr(orchestrator_module.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(orchestrator_module.time, 'sleep', clock.sleep)
    platform = _DeadlinePlatform(clock)
    monkeypatch.setattr(platform, 'verify_service_active', lambda name: clock.now >= startup_delay)
    instance = InstallerOrchestrator(
        platform=platform,
        options=InstallOptions(owner_name='Test', owner_password='SyntheticTestPassword'),
        paths=InstallerPaths.create(custom_root=tmp_path/'chief', home=tmp_path/'home'),
        logger=InstallerLogger(log_dir=tmp_path/'logs'), deployment_client=_DeploymentClient())
    instance.state_machine.current_step = StepId.LOCAL_DISCOVERY
    instance.state_machine.current_status = StepStatus.SUCCESS
    expected = '15-second deadline' if startup_delay == 10 else 'within 60 seconds'
    with pytest.raises(InstallerError, match=expected):
        instance._step_health_check()
    assert clock.now == pytest.approx(25 if startup_delay == 10 else 60)


@pytest.mark.parametrize("failure", [None, "certificate", "missing", "redirect", "starting"])
def test_https_routes_require_trust_and_expected_pages(tmp_path, monkeypatch, failure):
    clock = _Clock()
    monkeypatch.setattr(orchestrator_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(orchestrator_module.time, "sleep", clock.sleep)
    engine = InstallerOrchestrator(
        platform=_DeadlinePlatform(_Clock()),
        options=InstallOptions(owner_name="Test", owner_password="StrongPassword2026!",
                               public_host="chief-test.local", tls_mode="LOCAL"),
        paths=InstallerPaths.create(custom_root=tmp_path / "chief", home=tmp_path / "home"),
        logger=InstallerLogger(log_dir=tmp_path / "logs", session_id="https"),
        deployment_client=_DeploymentClient(),
    )
    trusted_context = object()
    def context(*, cafile):
        assert cafile == str(engine.paths.caddy_root / "data/caddy/pki/authorities/local/root.crt")
        if failure == "certificate":
            raise ValueError("invalid CA")
        if failure == "starting" and clock.now < 2:
            raise FileNotFoundError("CA not yet created")
        return trusted_context
    visited = []
    class Response:
        status = 404 if failure == "missing" else 302 if failure == "redirect" else 200
        def read(self): return b"ok"
    class Connection:
        def __init__(self, host, *, context, timeout):
            assert host == "chief-test.local"
            assert context is trusted_context
            assert 0 < timeout <= 5
            self.route = None
        def request(self, method, route, *, headers):
            assert method == "GET"
            assert headers == {"Host": "chief-test.local", "Connection": "close"}
            self.route = route
            visited.append(f"https://chief-test.local{route}")
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(orchestrator_module.ssl, "create_default_context", context)
    monkeypatch.setattr(orchestrator_module, "_LoopbackHTTPSConnection", Connection)
    if failure and failure != "starting":
        with pytest.raises(InstallerError, match="not ready"):
            engine._verify_https_routes()
        assert clock.now == 30
    else:
        engine._verify_https_routes()
        assert visited == ["https://chief-test.local" + p for p in ("/login", "/owner/", "/staff/", "/jobs/")]
        assert clock.now == (2 if failure == "starting" else 0)


def test_loopback_https_connection_uses_loopback_socket_and_hostname_sni(monkeypatch):
    raw_socket = object()
    wrapped_socket = object()
    calls = {}

    def create_connection(address, *, timeout, source_address):
        calls["address"] = address
        calls["timeout"] = timeout
        return raw_socket

    class Context:
        verify_mode = orchestrator_module.ssl.CERT_REQUIRED
        check_hostname = True
        def wrap_socket(self, sock, *, server_hostname):
            calls["raw_socket"] = sock
            calls["server_hostname"] = server_hostname
            return wrapped_socket

    monkeypatch.setattr(orchestrator_module.socket, "create_connection", create_connection)
    connection = orchestrator_module._LoopbackHTTPSConnection(
        "chief-test.local", context=Context(), timeout=3
    )
    connection.connect()
    assert calls == {
        "address": ("127.0.0.1", 443),
        "timeout": 3,
        "raw_socket": raw_socket,
        "server_hostname": "chief-test.local",
    }
    assert connection.sock is wrapped_socket
