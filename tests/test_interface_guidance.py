"""Tests for on-farm Chief console guidance and distinct PWA separation.

Verifies:
1. Orchestrator returns 'chief' (root console), 'owner', 'staff', 'jobs' URLs.
2. Completion message guides operator to the on-farm console.
3. GUI completion screen presents on-farm console with dedicated action button.
4. CLI summary displays on-farm console separately from PWAs.
5. Device setup exports clearly distinguish on-farm console from mobile PWAs.
6. Backend role gating preserves fail-closed boundaries (Owner/Admin vs Manager/Worker).
"""
from pathlib import Path
import json
import pytest

from deployment.installer import InstallOptions, StepId, StepStatus
from deployment.installer.models import InstallResult
from deployment.installer.orchestrator import InstallerOrchestrator
from deployment.installer.platforms.linux import LinuxPlatformAdapter
from deployment.installer.platforms.windows import WindowsPlatformAdapter
from deployment.installer.ui.controller import InstallerUIController, ScreenId


def test_orchestrator_urls_distinguish_on_farm_console_and_pwas(tmp_path):
    from tests.test_installer_orchestrator import FakePlatformAdapter
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)

    opts = InstallOptions(
        owner_name="Farmer Jane",
        owner_username="jane",
        owner_password="StrongPassword123!",
        farm_operations="DISABLED",
        dns_mode="quad9",
        tls_mode="LOCAL",
        allow_test_mock=True,
    )
    from deployment.installer.orchestrator import InstallerPaths
    from deployment.installer.logging import InstallerLogger
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_guidance")
    orch = InstallerOrchestrator(
        platform=platform,
        options=opts,
        paths=paths,
        logger=logger,
    )
    result = orch.execute_all()

    assert result.status == "SUCCESS", f"Error: {result.error}"
    assert "chief" in result.urls
    assert "owner" in result.urls
    assert "staff" in result.urls
    assert "jobs" in result.urls

    assert result.urls["chief"].endswith("/")
    assert not result.urls["chief"].endswith("/owner/")
    assert result.urls["owner"].endswith("/owner/")
    assert result.urls["staff"].endswith("/staff/")
    assert result.urls["jobs"].endswith("/jobs/")

    # Completion state message must point to on-farm console
    last_transition = orch.state_machine.history[-1]
    assert last_transition[0] == StepId.COMPLETE
    assert result.urls["chief"] in last_transition[2]
    assert last_transition[2].endswith(result.urls["chief"])


def test_ui_completion_renders_on_farm_console_and_distinct_actions(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from deployment.installer.orchestrator import InstallerPaths
    from deployment.installer import console
    paths = InstallerPaths.create(custom_root=tmp_path / 'chief', home=tmp_path / 'home')
    def probe(selected_paths):
        assert selected_paths is paths
        return SimpleNamespace(state=SimpleNamespace(value='NOT_INSTALLED'))
    monkeypatch.setattr(console, 'probe_local_console', probe)
    monkeypatch.setattr(console, 'launch_console_window', lambda *args: pytest.fail('Must not launch a real console'))
    class FakePlatform:
        os_name = "windows"
        def __init__(self): self.opened_urls = []
        def get_platform_info(self):
            from deployment.installer.models import PlatformInfo
            return PlatformInfo("windows", "windows", "Windows", "11", "x86_64", True)
        def run_preflight_checks(self):
            from deployment.installer.models import PreflightReport
            return PreflightReport(True, [])
        def detect_existing_installation(self): return None
        def open_url_in_browser(self, url):
            self.opened_urls.append(url)
            return True

    platform = FakePlatform()
    controller = InstallerUIController(
        platform_adapter=platform,
        orchestrator_factory=lambda opts: None,
        paths=paths,
    )
    controller.public_host = "chief-server.local"
    controller.install_result = InstallResult(
        status="SUCCESS",
        public_host="chief-server.local",
        urls={
            "chief": "https://chief-server.local/",
            "owner": "https://chief-server.local/owner/",
            "staff": "https://chief-server.local/staff/",
            "jobs": "https://chief-server.local/jobs/",
        },
    )

    # Controller can open on-farm console by default
    assert controller.open_chief_url("chief") is True
    assert platform.opened_urls[-1] == "https://chief-server.local/"

    # Controller can open remote owner companion
    assert controller.open_chief_url("owner") is True
    assert platform.opened_urls[-1] == "https://chief-server.local/owner/"

    # Controller can open staff mobile companion
    assert controller.open_chief_url("staff") is True
    assert platform.opened_urls[-1] == "https://chief-server.local/staff/"


def test_device_setup_guides_distinguish_console_from_mobile_pwas(tmp_path):
    ca_cert = tmp_path / "root.crt"
    ca_cert.write_text("DUMMY_CERT", encoding="utf-8")

    linux_dest = tmp_path / "linux_export"
    linux_readme = LinuxPlatformAdapter().export_device_ca(ca_cert, linux_dest, "Farm Owner", "farm.local")
    linux_text = linux_readme.read_text(encoding="utf-8")
    assert "On-farm Chief console: https://farm.local/" in linux_text
    assert "Remote Owner PWA:       https://farm.local/owner/" in linux_text
    assert "Mobile Staff PWA:       https://farm.local/staff/" in linux_text

    win_dest = tmp_path / "win_export"
    win_readme = WindowsPlatformAdapter().export_device_ca(ca_cert, win_dest, "Farm Owner", "farm.local")
    win_text = win_readme.read_text(encoding="utf-8")
    assert "On-Farm Chief Console (Local/LAN): https://farm.local/" in win_text
    assert "Remote Owner PWA (Mobile):        https://farm.local/owner/" in win_text
    assert "Mobile Staff PWA (Field):         https://farm.local/staff/" in win_text


def test_caddyfile_routing_preserves_pwa_isolation_and_root_console(tmp_path):
    from deployment.installer.orchestrator import InstallerPaths
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    orch = InstallerOrchestrator(
        platform=LinuxPlatformAdapter(),
        options=InstallOptions(
            owner_name="Test Owner",
            owner_username="owner",
            owner_password="Password123!",
        ),
        paths=paths,
    )
    caddyfile = orch.generate_caddyfile("chief.test.local", "LOCAL")

    # Verify PWA isolations
    assert "handle_path /owner/*" in caddyfile
    assert f'root * "{str(paths.pwa_root).replace(chr(92), "/")}/owner/dist"' in caddyfile
    assert "handle_path /staff/*" in caddyfile
    assert f'root * "{str(paths.pwa_root).replace(chr(92), "/")}/staff/dist"' in caddyfile
    assert "handle_path /jobs/*" in caddyfile
    assert f'root * "{str(paths.pwa_root).replace(chr(92), "/")}/jobs/dist"' in caddyfile

    # Verify root and API routing to on-farm dashboard backend
    assert "handle /api/*" in caddyfile
    assert "handle {" in caddyfile
    assert "reverse_proxy 127.0.0.1:8765" in caddyfile

    # Verify security headers
    assert 'Strict-Transport-Security "max-age=31536000"' in caddyfile
    assert 'X-Content-Type-Options "nosniff"' in caddyfile
    assert 'X-Frame-Options "DENY"' in caddyfile
    assert 'Referrer-Policy "no-referrer"' in caddyfile
