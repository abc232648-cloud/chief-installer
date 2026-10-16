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
    ACCEPTED_FIRST_RELEASE_MANIFEST,
    EventBus,
    InstallOptions,
    PlatformInfo,
    PreflightCheck,
    PreflightReport,
    ReleaseManifest,
    StepId,
    StepStatus,
)
from deployment.installer.contract import ChiefAgentDeploymentClient
from deployment.installer.logging import InstallerLogger
from deployment.installer.orchestrator import InstallerOrchestrator, InstallerPaths
from deployment.installer.platforms.base import PlatformAdapter


class AcceptancePlatformAdapter(PlatformAdapter):
    """Platform adapter for acceptance test that simulates Linux OS layer while allowing real contract APIs."""

    def __init__(self, username: str = "testowner"):
        self.username = username
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


def test_clean_host_acceptance_with_real_chief_deployment_contract(tmp_path):
    """End-to-end acceptance test executing real Chief Agent contract without mock fallback."""
    chief_agent_repo = Path(__file__).resolve().parents[2] / "chief-agent"
    assert (chief_agent_repo / "deployment" / "contract.py").exists(), "chief-agent repository not found"

    # Setup isolated test filesystem layout
    custom_root = tmp_path / "chief_share"
    custom_home = tmp_path / "chief_home"
    paths = InstallerPaths.create(custom_root=custom_root, home=custom_home)

    # Point tooling and release source to the local clean chief-agent repository
    paths.tooling = chief_agent_repo
    paths.release_source = chief_agent_repo
    paths.python = Path(sys.executable)

    # Set up real browser configuration in advance
    paths.browser_config.parent.mkdir(parents=True, exist_ok=True)
    import hashlib
    real_sha = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    browser_data = {
        "executable": sys.executable,
        "sha256": real_sha,
        "version": "130.0.0.0",
    }
    paths.browser_config.write_text(json.dumps(browser_data), encoding="utf-8")

    # Real deployment client wired to target tooling
    deployment_client = ChiefAgentDeploymentClient(
        agent_root=paths.tooling,
        python_bin=Path(sys.executable),
    )

    platform = AcceptancePlatformAdapter(username="testowner")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="acceptance_test")
    event_bus = EventBus()
    events = []
    event_bus.subscribe(events.append)

    # Explicit allow_test_mock=False to force real contract execution
    options = InstallOptions(
        owner_name="Acceptance Farm Ltd",
        owner_username="testowner",
        owner_password="StrongPassword2026!#",
        farm_operations="DISABLED",
        tls_mode="LOCAL",
        allow_test_mock=False,
    )

    orchestrator = InstallerOrchestrator(
        platform=platform,
        options=options,
        manifest=ACCEPTED_FIRST_RELEASE_MANIFEST,
        paths=paths,
        event_bus=event_bus,
        logger=logger,
        deployment_client=deployment_client,
    )

    # Pre-stage PWA builds so npm build is not required over network during test
    for app in ("owner", "staff", "jobs"):
        dist_dir = paths.pwa_root / app / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "index.html").write_text(f"<!DOCTYPE html><html><body>Chief {app}</body></html>", encoding="utf-8")

    # Pre-stage mock caddy binary to validate caddyfile handling without downloading external binary
    (paths.caddy_root / "bin").mkdir(parents=True, exist_ok=True)
    (paths.caddy_root / "bin" / "caddy").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    # Pre-stage mock caddy CA root certificate
    ca_dir = paths.caddy_root / "data" / "caddy" / "pki" / "authorities" / "local"
    ca_dir.mkdir(parents=True, exist_ok=True)
    (ca_dir / "root.crt").write_text("-----BEGIN CERTIFICATE-----\nACCEPTANCE-CA\n-----END CERTIFICATE-----\n", encoding="utf-8")

    # Execute full installation state machine
    result = orchestrator.execute_all()

    # 1. State machine completion
    assert result.status == "SUCCESS", f"Installation failed with: {result.error}"
    assert orchestrator.current_step == StepId.COMPLETE
    assert result.public_host == "chief-acceptance-farm-ltd.local"

    # 2. Real SOURCE_MANIFEST generated by contract
    assert paths.source_manifest.exists()
    source_manifest_data = json.loads(paths.source_manifest.read_text(encoding="utf-8"))
    assert "source_tree_sha256" in source_manifest_data
    assert "files" in source_manifest_data
    assert len(source_manifest_data["source_tree_sha256"]) == 64

    # 3. Real production database & instance config generated by prepare_production
    assert paths.config.exists()
    instance_config = json.loads(paths.config.read_text(encoding="utf-8"))
    assert "schema_sha256" in instance_config
    assert instance_config["dashboard_port"] == 8765
    assert instance_config["mode"] == "PRODUCTION"

    assert paths.identity_config.exists()
    identity_data = json.loads(paths.identity_config.read_text(encoding="utf-8"))
    assert identity_data["owner_username"] == "testowner"

    # 4. Real initial backup generated by create_install_backup
    assert paths.backup.exists()
    complete_file = paths.backup / "COMPLETE"
    assert complete_file.exists()
    assert len(complete_file.read_text(encoding="utf-8").strip()) == 64

    # 5. Real managed installation provisioned by bootstrap_installation
    assert paths.managed.exists()
    assert (paths.managed / "active.json").exists()
    assert (paths.managed / "installation.json").exists()
    assert (paths.managed / "releases" / source_manifest_data["source_tree_sha256"] / "SOURCE_MANIFEST.json").exists()

    # 6. Real systemd service definitions generated by write_service_definitions
    assert (paths.unit_stage / "chief-agent-dashboard.service").exists()
    assert (paths.unit_stage / "chief-agent-worker.service").exists()
    assert (paths.unit_stage / "chief-agent-scheduler.service").exists()
    dashboard_content = (paths.unit_stage / "chief-agent-dashboard.service").read_text(encoding="utf-8")
    assert "ConditionUser=testowner" in dashboard_content
    assert "Description=Chief Agent dashboard" in dashboard_content

    # 7. Caddyfile and TLS service unit generated
    assert paths.caddyfile.exists()
    caddyfile_text = paths.caddyfile.read_text(encoding="utf-8")
    assert "chief-acceptance-farm-ltd.local" in caddyfile_text
    assert "reverse_proxy 127.0.0.1:8765" in caddyfile_text
    assert (paths.unit_dest / "chief-tls-proxy.service").exists()

    # 8. Security verification: password must NEVER be logged anywhere
    log_content = logger.log_file.read_text(encoding="utf-8")
    assert "StrongPassword2026!#" not in log_content
    assert "[REDACTED]" in log_content
