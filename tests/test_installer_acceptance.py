"""End-to-end clean-host acceptance test for Chief Unified Installer.

Executes the full installation state machine against the real Chief Agent
deployment contract APIs without mock fallbacks (allow_test_mock=False).
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Callable, Dict, List, Optional, Tuple
import pytest

from deployment.installer import (
    EventBus,
    InstallOptions,
    PlatformInfo,
    PreflightCheck,
    PreflightReport,
    ReleaseManifest,
    StepId,
    StepStatus,
    parse_manifest,
)
from deployment.installer.contract import ChiefAgentDeploymentClient
from deployment.installer.logging import InstallerLogger
from deployment.installer.orchestrator import InstallerOrchestrator, InstallerPaths
from deployment.installer.platforms.base import PlatformAdapter


class AcceptancePlatformAdapter(PlatformAdapter):
    """Platform adapter for acceptance test that simulates Linux OS layer while allowing real contract APIs."""

    def __init__(self, username: str = "testowner"):
        self.username = username
        self.os_name = "linux"
        self.calls: List[str] = []
        self.installed_units: List[Path] = []

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

    def get_current_username(self) -> str:
        self.calls.append("get_current_username")
        return self.username

    def run_preflight_checks(self) -> PreflightReport:
        self.calls.append("run_preflight_checks")
        return PreflightReport(
            platform=self.get_platform_info(),
            checks=[
                PreflightCheck(name="os_supported", passed=True, message="Acceptance Xubuntu platform"),
                PreflightCheck(name="disk_space", passed=True, message="100GB available"),
            ],
            passed=True,
        )

    def detect_existing_installation(self) -> Tuple[bool, List[str]]:
        self.calls.append("detect_existing_installation")
        return False, []

    def archive_existing_installation(
        self,
        archive_root: Path,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Path:
        self.calls.append("archive_existing_installation")
        return archive_root

    def install_system_dependencies(
        self,
        progress_callback: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        self.calls.append("install_system_dependencies")
        if progress_callback:
            progress_callback("Acceptance test packages installed", 1.0)

    def enable_user_linger(self, username: Optional[str] = None) -> None:
        self.calls.append("enable_user_linger")

    def configure_local_discovery(
        self,
        owner_name: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        self.calls.append("configure_local_discovery")
        return "chief-acceptance-farm-ltd.local"

    def set_executable_capability(self, executable: Path, capability: str) -> None:
        self.calls.append(f"setcap:{capability}")

    def install_service_units(self, stage_dir: Path, dest_dir: Path) -> None:
        self.calls.append("install_service_units")
        dest_dir.mkdir(parents=True, exist_ok=True)
        for unit in stage_dir.glob("*.service"):
            dest_file = dest_dir / unit.name
            dest_file.write_text(unit.read_text(encoding="utf-8"), encoding="utf-8")
            self.installed_units.append(dest_file)

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
        readme.write_text(f"Acceptance device setup for {owner_name} at {public_host}", encoding="utf-8")
        return readme

    def open_url_in_browser(self, url: str) -> bool:
        self.calls.append(f"open_url:{url}")
        return True

    def probe_health_endpoint(self, url: str, timeout: float = 2.0) -> bool:
        self.calls.append(f"probe_health_endpoint:{url}")
        return True


def _find_chief_agent_repo() -> Optional[Path]:
    if os.environ.get("CHIEF_AGENT_ROOT"):
        p = Path(os.environ["CHIEF_AGENT_ROOT"])
        if (p / "deployment" / "contract.py").exists():
            return p
    sibling = Path(__file__).resolve().parents[2] / "chief-agent"
    if (sibling / "deployment" / "contract.py").exists():
        return sibling
    child = Path(__file__).resolve().parents[1] / "chief-agent"
    if (child / "deployment" / "contract.py").exists():
        return child
    return None


def _dns_review_manifest():
    return parse_manifest(Path(__file__).resolve().parents[1] / "review-dns-manifest.json")


def test_clean_host_acceptance_with_real_chief_deployment_contract(tmp_path, monkeypatch):
    """Real Chief Agent deployment contract integration acceptance test (allow_test_mock=False)."""
    if sys.version_info < (3, 11):
        pytest.skip("chief-agent requires Python 3.11+ (hashlib.file_digest); skipping acceptance test on Python 3.10")

    chief_agent_repo = _find_chief_agent_repo()
    if not chief_agent_repo:
        pytest.fail("Mandatory acceptance test failed: chief-agent peer repository not found.")

    if not (chief_agent_repo / "deployment" / "contract.py").exists():
        pytest.fail(f"Mandatory acceptance test failed: deployment/contract.py not found in {chief_agent_repo}")

    try:
        import argon2  # noqa: F401
        import cryptography  # noqa: F401
    except ImportError as exc:
        pytest.fail(f"Mandatory acceptance test failed: Chief Agent dependencies missing ({exc})")

    custom_root = (tmp_path / "chief_share").resolve()
    custom_home = (tmp_path / "chief_home").resolve()
    paths = InstallerPaths.create(custom_root=custom_root, home=custom_home)

    paths.tooling = chief_agent_repo.resolve()
    paths.release_source = chief_agent_repo.resolve()
    paths.python = Path(sys.executable)

    browser_bin = (custom_root / "browser" / ("chrome.exe" if os.name == "nt" else "chrome")).resolve()
    browser_bin.parent.mkdir(parents=True, exist_ok=True)
    browser_bin.write_bytes(b"CHIEF-BROWSER-BINARY-REGULAR-FILE\x00\x01\x02\x03\x04\x05")
    try:
        browser_bin.chmod(0o755)
    except Exception:
        pass

    import hashlib
    real_sha = hashlib.sha256(browser_bin.read_bytes()).hexdigest()
    browser_data = {
        "executable": str(browser_bin),
        "sha256": real_sha,
        "version": "130.0.0.0",
    }
    paths.browser_config.parent.mkdir(parents=True, exist_ok=True)
    paths.browser_config.write_text(json.dumps(browser_data), encoding="utf-8")

    deployment_client = ChiefAgentDeploymentClient(
        agent_root=paths.tooling,
        python_bin=paths.python,
    )

    platform = AcceptancePlatformAdapter(username="testowner")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="acceptance_test")
    event_bus = EventBus()
    events = []
    event_bus.subscribe(events.append)

    options = InstallOptions(
        owner_name="Acceptance Farm Ltd",
        owner_username="testowner",
        owner_password="StrongPassword2026!#",
        farm_operations="DISABLED",
        tls_mode="LOCAL",
        dns_mode="quad9",
        allow_test_mock=False,
    )

    orchestrator = InstallerOrchestrator(
        platform=platform,
        options=options,
        manifest=_dns_review_manifest(),
        paths=paths,
        event_bus=event_bus,
        logger=logger,
        deployment_client=deployment_client,
    )

    for app in ("owner", "staff", "jobs"):
        dist_dir = paths.pwa_root / app / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "index.html").write_text(f"<!DOCTYPE html><html><body>Chief {app}</body></html>", encoding="utf-8")

    (paths.caddy_root / "bin").mkdir(parents=True, exist_ok=True)
    caddy_file = paths.caddy_root / "bin" / "caddy"
    caddy_file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    try:
        caddy_file.chmod(0o755)
    except Exception:
        pass

    ca_dir = paths.caddy_root / "data" / "caddy" / "pki" / "authorities" / "local"
    ca_dir.mkdir(parents=True, exist_ok=True)
    (ca_dir / "root.crt").write_text("-----BEGIN CERTIFICATE-----\nACCEPTANCE-CA\n-----END CERTIFICATE-----\n", encoding="utf-8")

    # This contract fixture uses a fake Caddy binary/CA; real TLS is qualified
    # separately against the native installation, not by this fixture.
    monkeypatch.setattr(orchestrator, "_verify_https_routes", lambda: None)
    result = orchestrator.execute_all()

    assert result.status == "SUCCESS", f"Installation failed with: {result.error}"
    assert orchestrator.current_step == StepId.COMPLETE
    assert result.public_host == "chief-acceptance-farm-ltd.local"

    assert paths.source_manifest.exists()
    source_manifest_data = json.loads(paths.source_manifest.read_text(encoding="utf-8"))
    assert "source_tree_sha256" in source_manifest_data
    assert "files" in source_manifest_data
    assert len(source_manifest_data["source_tree_sha256"]) == 64

    assert paths.config.exists()
    instance_config = json.loads(paths.config.read_text(encoding="utf-8"))
    assert "schema_sha256" in instance_config
    assert instance_config["dashboard_port"] == 8765
    assert instance_config["mode"] == "PRODUCTION"
    assert instance_config["dns_mode"] == "quad9"

    assert paths.identity_config.exists()
    identity_data = json.loads(paths.identity_config.read_text(encoding="utf-8"))
    assert identity_data["owner_username"] == "testowner"

    assert paths.backup.exists()
    complete_file = paths.backup / "COMPLETE"
    assert complete_file.exists()
    assert len(complete_file.read_text(encoding="utf-8").strip()) == 64

    assert paths.managed.exists()
    assert (paths.managed / "active.json").exists()
    assert (paths.managed / "installation.json").exists()
    assert (paths.managed / "releases" / source_manifest_data["source_tree_sha256"] / "SOURCE_MANIFEST.json").exists()

    assert (paths.unit_stage / "chief-agent-dashboard.service").exists()
    assert (paths.unit_stage / "chief-agent-worker.service").exists()
    assert (paths.unit_stage / "chief-agent-scheduler.service").exists()
    dashboard_content = (paths.unit_stage / "chief-agent-dashboard.service").read_text(encoding="utf-8")
    assert "ConditionUser=testowner" in dashboard_content
    assert "Description=Chief Agent dashboard" in dashboard_content

    assert paths.caddyfile.exists()
    caddyfile_text = paths.caddyfile.read_text(encoding="utf-8")
    assert "chief-acceptance-farm-ltd.local" in caddyfile_text
    assert "reverse_proxy 127.0.0.1:8765" in caddyfile_text
    assert (paths.unit_dest / "chief-tls-proxy.service").exists()

    log_content = logger.log_file.read_text(encoding="utf-8")
    assert "StrongPassword2026!#" not in log_content
    assert "[REDACTED]" in log_content
