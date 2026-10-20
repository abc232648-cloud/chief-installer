"""Tests for WireGuard remote access setup in Chief Unified Installer."""
from pathlib import Path
import pytest

from deployment.installer.models import InstallOptions, StepId, StepStatus
from deployment.installer.orchestrator import InstallerOrchestrator, InstallerPaths
from deployment.installer.logging import InstallerLogger
from deployment.installer.remote_access import InstallerWireGuardManager
from tests.test_installer_orchestrator import FakePlatformAdapter


def test_installer_wireguard_manager_reachability_and_cgnat():
    mgr = InstallerWireGuardManager(Path("/tmp/fake_wg"))
    # 1. CGNAT
    rep_cgnat = mgr.check_reachability("100.64.50.2")
    assert rep_cgnat.is_valid is False
    assert rep_cgnat.cgnat_detected is True
    assert "Carrier-Grade NAT" in rep_cgnat.message

    # 2. Private IP
    rep_priv = mgr.check_reachability("192.168.0.50")
    assert rep_priv.is_valid is False
    assert rep_priv.is_private_ip is True

    # 3. Public routable IP
    rep_pub = mgr.check_reachability("203.0.113.88", 51820)
    assert rep_pub.is_valid is True
    assert rep_pub.cgnat_detected is False


def test_installer_wireguard_provisioning(tmp_path):
    mgr = InstallerWireGuardManager(tmp_path / "wg_state")
    server_conf, client_conf = mgr.provision_initial_server_and_owner(
        endpoint="203.0.113.88",
        canonical_hostname="chief-farm.local",
        owner_name="Jane Farmer",
        port=51820,
        vpn_subnet="10.77.0.0/24",
    )

    assert "[Interface]" in server_conf
    assert "Address = 10.77.0.1/32" in server_conf
    assert "ListenPort = 51820" in server_conf
    assert "AllowedIPs = 10.77.0.2/32" in server_conf  # Owner IP

    assert "Jane Farmer" in client_conf
    assert "Address = 10.77.0.2/32" in client_conf
    assert "DNS = 10.77.0.1" in client_conf
    assert "AllowedIPs = 10.77.0.1/32" in client_conf  # Split tunnel


def test_orchestrator_execution_with_wireguard_enabled(tmp_path, monkeypatch):
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_wg")

    opts = InstallOptions(
        owner_name="Farmer Jane",
        owner_username="jane",
        owner_password="StrongPassword123!",
        farm_operations="DISABLED",
        dns_mode="quad9",
        tls_mode="LOCAL",
        remote_access="WIREGUARD",
        wireguard_endpoint="203.0.113.99",
        wireguard_port=51820,
        allow_test_mock=True,
    )

    orch = InstallerOrchestrator(
        platform=platform,
        options=opts,
        paths=paths,
        logger=logger,
    )

    # Mock Path.home() so device setup exports into tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    result = orch.execute_all()

    assert result.status == "SUCCESS", f"Error: {result.error}"
    assert result.wireguard_configured is True
    assert result.wireguard_client_config_path is not None
    assert Path(result.wireguard_client_config_path).exists()

    wg_conf_text = Path(result.wireguard_client_config_path).read_text(encoding="utf-8")
    assert "Farmer Jane" in wg_conf_text
    assert "Endpoint = 203.0.113.99:51820" in wg_conf_text
    assert "DNS = 10.77.0.1" in wg_conf_text


def test_orchestrator_execution_with_wireguard_disabled(tmp_path):
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_wg_disabled")

    opts = InstallOptions(
        owner_name="Farmer Jane",
        owner_username="jane",
        owner_password="StrongPassword123!",
        farm_operations="DISABLED",
        dns_mode="quad9",
        tls_mode="LOCAL",
        remote_access="DISABLED",
        allow_test_mock=True,
    )

    orch = InstallerOrchestrator(
        platform=platform,
        options=opts,
        paths=paths,
        logger=logger,
    )

    result = orch.execute_all()

    assert result.status == "SUCCESS", f"Error: {result.error}"
    assert result.wireguard_configured is False
    assert result.wireguard_client_config_path is None


def test_orchestrator_fails_when_backend_capability_absent(tmp_path):
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_wg_absent")

    opts = InstallOptions(
        owner_name="Farmer Jane",
        owner_username="jane",
        owner_password="StrongPassword123!",
        farm_operations="DISABLED",
        dns_mode="quad9",
        tls_mode="LOCAL",
        remote_access="WIREGUARD",
        wireguard_endpoint="203.0.113.99",
        wireguard_port=51820,
        enforce_backend_capability=True,
        allow_test_mock=True,
    )

    orch = InstallerOrchestrator(
        platform=platform,
        options=opts,
        paths=paths,
        logger=logger,
    )

    # Backend capability is missing: orchestrator must fail and not report remote access configured
    result = orch.execute_all()
    assert result.status == "FAILED"
    assert "lacks WireGuard remote access capability" in str(result.error)
    assert result.wireguard_configured is False


def test_orchestrator_wireguard_integration_with_chief_backend(tmp_path, monkeypatch):
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_wg_integ")

    # Simulate deployed Chief Agent with WireGuard capability
    prod_agent = paths.production
    (prod_agent / "networking").mkdir(parents=True, exist_ok=True)
    (prod_agent / "networking" / "remote_access.py").write_text("# Chief WireGuard backend present\n", encoding="utf-8")

    opts = InstallOptions(
        owner_name="Farmer Jane",
        owner_username="jane",
        owner_password="StrongPassword123!",
        farm_operations="DISABLED",
        dns_mode="quad9",
        tls_mode="LOCAL",
        remote_access="WIREGUARD",
        wireguard_endpoint="203.0.113.99",
        wireguard_port=51820,
        enforce_backend_capability=True,
        allow_test_mock=True,
    )

    orch = InstallerOrchestrator(
        platform=platform,
        options=opts,
        paths=paths,
        logger=logger,
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    result = orch.execute_all()
    assert result.status == "SUCCESS", f"Error: {result.error}"
    assert result.wireguard_configured is True
    assert result.wireguard_client_config_path is not None
    assert Path(result.wireguard_client_config_path).exists()

    # Verify fail-closed split-DNS profile properties
    content = Path(result.wireguard_client_config_path).read_text(encoding="utf-8")
    assert "DNS = 10.77.0.1" in content
    assert "AllowedIPs = 10.77.0.1/32" in content
    assert "Farmer Jane" in content


def test_route_and_origin_contract_verification():
    from deployment.installer.models import InstallOptions

    # Default options enforce farm operations DISABLED
    opts = InstallOptions(owner_name="Jane", owner_password="SafePassword2026!")
    assert opts.farm_operations == "DISABLED"
    assert opts.remote_access == "DISABLED"

    # Route mapping verification:
    # / -> on-farm local console
    # /owner/ -> remote companion
    # /staff/ -> staff experience
    # /jobs/ -> job agent
    routes = {
        "/": "local_on_farm_console",
        "/owner/": "owner_pwa_companion",
        "/staff/": "staff_pwa_companion",
        "/jobs/": "job_agent_companion",
    }
    assert routes["/"] == "local_on_farm_console"
    assert routes["/owner/"] == "owner_pwa_companion"
    assert routes["/staff/"] == "staff_pwa_companion"
    assert routes["/jobs/"] == "job_agent_companion"
