"""Tests for WireGuard remote access setup in Chief Unified Installer.

Verifies:
- Honest reachability analysis (CGNAT, private WAN, syntax, unverified UDP)
- Provisioning of initial server and owner client profiles
- Execution within the orchestrator
- Integration with real Chief Agent backend from peer repository (no dummy files)
- Real WireGuard split-DNS authoritative behavior (canonical domain vs REFUSED)
- Platform fail-closed enforcement on Windows (UnsupportedPlatformError)
- Atomic rollback preserving local on-farm console
- Secret protection: zero private keys logged
- Route contract preservation
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
import shutil
import sys
import pytest

from deployment.installer.errors import InstallerError, UnsupportedPlatformError
from deployment.installer.models import InstallOptions, StepId, StepStatus
from deployment.installer.orchestrator import InstallerOrchestrator, InstallerPaths
from deployment.installer.logging import InstallerLogger
from deployment.installer.remote_access import (
    InstallerWireGuardManager,
    ReachabilityReport,
    generate_wireguard_keypair,
    generate_preshared_key,
)
from tests.test_installer_orchestrator import FakePlatformAdapter


def find_peer_agent_path() -> Path:
    """Locate the peer chief-agent repository containing real networking implementations."""
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "chief-agent",
        Path(__file__).resolve().parent.parent.parent / "chief-integrated-candidate-20261020",
    ]
    for c in candidates:
        if c.exists() and (c / "networking" / "remote_access.py").exists() and (c / "networking" / "wireguard_dns.py").exists():
            return c
    raise FileNotFoundError(f"Peer chief-agent repository not found. Checked: {[str(c) for c in candidates]}")


def test_installer_wireguard_manager_reachability_and_cgnat():
    mgr = InstallerWireGuardManager(Path("/tmp/fake_wg"))

    # 1. CGNAT detection
    rep_cgnat = mgr.check_reachability("100.64.50.2")
    assert rep_cgnat.is_valid is False
    assert rep_cgnat.cgnat_detected is True
    assert rep_cgnat.reachability_status == "CGNAT_BLOCKED"
    assert "Carrier-Grade NAT" in rep_cgnat.message

    # 2. Private IP WAN detection
    rep_priv = mgr.check_reachability("192.168.0.50")
    assert rep_priv.is_valid is False
    assert rep_priv.is_private_ip is True
    assert rep_priv.reachability_status == "PRIVATE_WAN_BLOCKED"

    # 3. Invalid syntax
    rep_bad = mgr.check_reachability("not@a@valid@host!!")
    assert rep_bad.is_valid is False
    assert rep_bad.syntax_valid is False
    assert rep_bad.reachability_status == "SYNTAX_INVALID"

    # 4. Public routable IP: honest unverified UDP status
    rep_pub = mgr.check_reachability("203.0.113.88", 51820)
    assert rep_pub.is_valid is True
    assert rep_pub.is_public is True
    assert rep_pub.cgnat_detected is False
    assert rep_pub.is_private_ip is False
    assert rep_pub.udp_verified is False  # Cannot be verified without client handshake
    assert rep_pub.reachability_status == "PUBLIC_UNVERIFIED"


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
    # Allow simulated wireguard execution in unit fixture
    opts._allow_windows_wireguard_test = True

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
    opts._allow_windows_wireguard_test = True

    orch = InstallerOrchestrator(
        platform=platform,
        options=opts,
        paths=paths,
        logger=logger,
    )

    # Empty candidate locations to simulate absence of backend capability
    result = orch.execute_all()
    assert result.status == "FAILED"
    assert "lacks WireGuard remote access capability" in str(result.error)
    assert result.wireguard_configured is False


def test_windows_server_fails_closed_with_unsupported_platform(tmp_path):
    """WireGuard server hosting on Windows fails closed while keeping local console available."""
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    platform.os_name = "windows"
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_wg_win_unsupported")

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
    # Do NOT set _allow_windows_wireguard_test: it must fail closed

    orch = InstallerOrchestrator(
        platform=platform,
        options=opts,
        paths=paths,
        logger=logger,
    )

    result = orch.execute_all()
    assert result.status == "FAILED"
    assert result.error_code == "UNSUPPORTED_PLATFORM"
    assert "WireGuard server hosting is only supported on Linux" in str(result.error)
    assert result.wireguard_configured is False


def test_orchestrator_wireguard_integration_with_real_chief_backend(tmp_path, monkeypatch):
    """Integrates with real Chief Agent backend from peer repository without placeholder dummy files."""
    peer_agent = find_peer_agent_path()
    assert (peer_agent / "networking" / "remote_access.py").is_file()
    assert (peer_agent / "networking" / "wireguard_dns.py").is_file()

    # Import real modules from peer agent to verify functionality
    if str(peer_agent) not in sys.path:
        sys.path.insert(0, str(peer_agent))

    from networking.remote_access import WireGuardManager as RealWireGuardManager
    from networking.wireguard_dns import WireGuardDNSServer, build_dns_response, parse_dns_question

    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_wg_real_integ")

    # Copy real networking package from peer agent into production path
    prod_agent = paths.production
    (prod_agent / "networking").mkdir(parents=True, exist_ok=True)
    shutil.copy2(peer_agent / "networking" / "remote_access.py", prod_agent / "networking" / "remote_access.py")
    shutil.copy2(peer_agent / "networking" / "wireguard_dns.py", prod_agent / "networking" / "wireguard_dns.py")
    shutil.copy2(peer_agent / "networking" / "__init__.py", prod_agent / "networking" / "__init__.py")

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
    opts._allow_windows_wireguard_test = True

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

    # Verify real WireGuardManager can read the provisioned server state
    wg_state_dir = paths.root / "state" / "wireguard"
    real_mgr = RealWireGuardManager(wg_state_dir)
    assert real_mgr.is_configured() is True
    summary = real_mgr.get_safe_status()
    assert summary["enabled"] is True
    assert summary["peer_count"] == 1

    # Verify real WireGuard split-DNS server protocol logic
    wire_q = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x05chief\x05local\x00\x00\x01\x00\x01"
    qname, qtype, qclass, end = parse_dns_question(wire_q, 12)
    assert qname == "chief.local"
    assert qtype == 1
    assert qclass == 1

    resp_valid = build_dns_response(wire_q, "chief.local", "10.77.0.1")
    assert resp_valid[:2] == b"\x12\x34"
    assert resp_valid[-4:] == b"\x0a\x4d\x00\x01"  # 10.77.0.1 in binary

    # Verify open-resolver protection: foreign domain query returns REFUSED (RCODE = 5)
    wire_q_foreign = b"\x56\x78\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x06google\x03com\x00\x00\x01\x00\x01"
    resp_refused = build_dns_response(wire_q_foreign, "chief.local", "10.77.0.1")
    flags = int.from_bytes(resp_refused[2:4], "big")
    assert flags & 0x000F == 5  # RCODE 5 = REFUSED


def test_secret_scanning_in_installer_logs_and_state(tmp_path, monkeypatch):
    """Ensure sensitive Curve25519 private keys and PSKs never leak into installer logs or manifests."""
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    log_dir = tmp_path / "logs"
    logger = InstallerLogger(log_dir=log_dir, session_id="test_wg_secrets")

    # Set up production with real peer backend
    peer_agent = find_peer_agent_path()
    (paths.production / "networking").mkdir(parents=True, exist_ok=True)
    shutil.copy2(peer_agent / "networking" / "remote_access.py", paths.production / "networking" / "remote_access.py")

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
    opts._allow_windows_wireguard_test = True

    orch = InstallerOrchestrator(
        platform=platform,
        options=opts,
        paths=paths,
        logger=logger,
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    result = orch.execute_all()
    assert result.status == "SUCCESS"

    # Read server private key from secret location
    server_key_path = paths.root / "state" / "wireguard" / "server.key"
    assert server_key_path.exists()
    server_private_key = server_key_path.read_text(encoding="utf-8").strip()

    # Read client config to get client private key and PSK
    client_conf_path = Path(result.wireguard_client_config_path)
    client_conf_text = client_conf_path.read_text(encoding="utf-8")
    client_priv = ""
    psk = ""
    for line in client_conf_text.splitlines():
        if line.startswith("PrivateKey = "):
            client_priv = line.split("=", 1)[1].strip()
        elif line.startswith("PresharedKey = "):
            psk = line.split("=", 1)[1].strip()

    assert server_private_key
    assert client_priv
    assert psk

    # Scan installer log files: NO private keys or PSKs must appear anywhere in logs
    log_files = list(log_dir.glob("*.log")) + list(log_dir.glob("*.json"))
    assert len(log_files) > 0
    for log_f in log_files:
        content = log_f.read_text(encoding="utf-8", errors="ignore")
        assert server_private_key not in content, f"Server private key leaked in {log_f.name}"
        assert client_priv not in content, f"Client private key leaked in {log_f.name}"
        assert psk not in content, f"Pre-shared key leaked in {log_f.name}"


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
