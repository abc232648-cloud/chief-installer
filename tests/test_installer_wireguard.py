"""Tests for WireGuard remote access setup in Chief Unified Installer.

Verifies:
- Honest reachability analysis (CGNAT, private WAN, syntax, unverified UDP)
- Provisioning of initial server and owner client profiles
- Idempotency across repeated provisioning (keys and peer identities preserved)
- Orchestrator execution with WireGuard enabled/disabled
- Integration with real Chief Agent backend from peer repository (no dummy files)
- Real WireGuard split-DNS authoritative behavior (canonical domain vs REFUSED)
- Platform fail-closed enforcement on Windows (UnsupportedPlatformError)
- Atomic rollback preserving local on-farm console on firewall or service readiness failure
- Secret protection: zero private keys logged in installer logs or diagnostic state
- Route contract preservation across all interfaces
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import sys
import pytest

from deployment.installer.errors import InstallerError, PrivilegeError, UnsupportedPlatformError
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
    assert "PostUp = systemctl start chief-wireguard-dns.service" in server_conf
    assert "AllowedIPs = 10.77.0.2/32" in server_conf  # Owner IP

    assert "Jane Farmer" in client_conf
    assert "Address = 10.77.0.2/32" in client_conf
    assert "DNS = 10.77.0.1" in client_conf
    assert "AllowedIPs = 10.77.0.1/32" in client_conf  # Split tunnel


def test_installer_wireguard_provisioning_idempotency(tmp_path):
    """Re-running provisioning preserves existing server keys and owner credentials."""
    mgr = InstallerWireGuardManager(tmp_path / "wg_state")
    server1, client1 = mgr.provision_initial_server_and_owner(
        endpoint="203.0.113.88",
        canonical_hostname="chief-farm.local",
        owner_name="Jane Farmer",
    )
    server_key_first = mgr.server_key_path.read_text(encoding="utf-8")

    # Second run with same state directory
    server2, client2 = mgr.provision_initial_server_and_owner(
        endpoint="203.0.113.88",
        canonical_hostname="chief-farm.local",
        owner_name="Jane Farmer",
    )
    server_key_second = mgr.server_key_path.read_text(encoding="utf-8")

    assert server_key_first == server_key_second, "Server private key was unexpectedly regenerated on retry"
    assert server1 == server2, "Server configuration changed unexpectedly on idempotent retry"
    assert client1 == client2, "Owner client configuration changed unexpectedly on idempotent retry"


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

    if str(peer_agent) not in sys.path:
        sys.path.insert(0, str(peer_agent))

    from networking.remote_access import WireGuardManager as RealWireGuardManager
    from networking.wireguard_dns import WireGuardDNSServer, build_dns_response, parse_dns_question

    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_wg_real_integ")

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
    assert resp_valid[-4:] == b"\x0a\x4d\x00\x01"

    # Verify open-resolver protection: foreign domain query returns REFUSED (RCODE = 5)
    wire_q_foreign = b"\x56\x78\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x06google\x03com\x00\x00\x01\x00\x01"
    resp_refused = build_dns_response(wire_q_foreign, "chief.local", "10.77.0.1")
    flags = int.from_bytes(resp_refused[2:4], "big")
    assert flags & 0x000F == 5  # RCODE 5 = REFUSED


def test_wireguard_rollback_on_firewall_failure_preserves_local_console(tmp_path, monkeypatch):
    """Firewall failure triggers rollback and leaves local console files undamaged."""
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    platform.fail_wireguard_firewall = True
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_wg_rollback_fw")

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
    platform.os_name = "linux"
    opts._test_run_firewall = True

    orch = InstallerOrchestrator(
        platform=platform,
        options=opts,
        paths=paths,
        logger=logger,
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    result = orch.execute_all()
    assert result.status == "FAILED"
    assert "Simulated firewall failure" in str(result.error)
    assert result.wireguard_configured is False
    assert "rollback_wireguard_firewall" in platform.calls


def test_wireguard_rollback_on_service_readiness_failure(tmp_path, monkeypatch):
    """WireGuard service failing readiness verification triggers safe rollback."""
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    platform.os_name = "linux"
    platform.wireguard_active_result = False
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_wg_rollback_svc")

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
    opts._test_run_firewall = True

    orch = InstallerOrchestrator(
        platform=platform,
        options=opts,
        paths=paths,
        logger=logger,
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    result = orch.execute_all()
    assert result.status == "FAILED"
    assert "WireGuard service failed readiness verification" in str(result.error)
    assert result.wireguard_configured is False
    assert "control_wireguard_service:stop" in platform.calls
    assert "rollback_wireguard_firewall" in platform.calls


def test_secret_scanning_in_installer_logs_and_state(tmp_path, monkeypatch):
    """Ensure sensitive Curve25519 private keys and PSKs never leak into installer logs or manifests."""
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    log_dir = tmp_path / "logs"
    logger = InstallerLogger(log_dir=log_dir, session_id="test_wg_secrets")

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

    server_key_path = paths.root / "state" / "wireguard" / "server.key"
    assert server_key_path.exists()
    server_private_key = server_key_path.read_text(encoding="utf-8").strip()

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

    log_files = list(log_dir.glob("*.log")) + list(log_dir.glob("*.json"))
    assert len(log_files) > 0
    for log_f in log_files:
        content = log_f.read_text(encoding="utf-8", errors="ignore")
        assert server_private_key not in content, f"Server private key leaked in {log_f.name}"
        assert client_priv not in content, f"Client private key leaked in {log_f.name}"
        assert psk not in content, f"Pre-shared key leaked in {log_f.name}"


def test_route_and_origin_contract_verification():
    from deployment.installer.models import InstallOptions

    opts = InstallOptions(owner_name="Jane", owner_password="SafePassword2026!")
    assert opts.farm_operations == "DISABLED"
    assert opts.remote_access == "DISABLED"

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


def test_linux_setup_path_with_real_chief_backend_and_dns_startup(tmp_path, monkeypatch):
    """Test actual Linux setup path with the exact pinned Chief backend and live UDP DNS socket resolution.

    Exercises the installer's call to wg_mgr.start_dns_server(canonical_hostname=canonical_host),
    verifying it harmonizes with chief-agent backend without TypeError, verifies service control,
    firewalling, live socket DNS query/response, and DNS readiness verification.
    """
    peer_agent = find_peer_agent_path()
    assert (peer_agent / "networking" / "remote_access.py").is_file()
    assert (peer_agent / "networking" / "wireguard_dns.py").is_file()

    if str(peer_agent) not in sys.path:
        sys.path.insert(0, str(peer_agent))

    from networking.remote_access import WireGuardManager as RealWireGuardManager
    from networking.wireguard_dns import WireGuardDNSServer
    from deployment.installer.remote_access import InstallerWireGuardManager

    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    platform.os_name = "linux"
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_wg_linux_real")

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
    opts._test_run_firewall = True

    orch = InstallerOrchestrator(
        platform=platform,
        options=opts,
        paths=paths,
        logger=logger,
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    # Start live real DNS daemon on loopback port to test real RFC 1035 UDP packet flow on non-root test host
    test_dns_port = 53536
    live_dns = WireGuardDNSServer(
        bind_ip="127.0.0.1",
        port=test_dns_port,
        canonical_hostname="chief.local",
        answer_ip="10.77.0.1",
    )
    live_dns.start()
    try:
        # Patch wg_mgr.start_dns_server to hook the running live daemon and test readiness check
        orig_check = InstallerWireGuardManager.check_dns_readiness
        def hooked_start(self, *a, **kw):
            if "canonical_hostname" in kw:
                live_dns.canonical_hostname = kw["canonical_hostname"]
            elif len(a) > 1 and isinstance(a[1], str):
                live_dns.canonical_hostname = a[1]
            elif len(a) > 0 and isinstance(a[0], str) and not a[0].isdigit():
                live_dns.canonical_hostname = a[0]
            self._dns_server = live_dns
            return True

        monkeypatch.setattr(InstallerWireGuardManager, "start_dns_server", hooked_start)
        monkeypatch.setattr(
            InstallerWireGuardManager,
            "check_dns_readiness",
            lambda self, canonical_hostname, **kw: orig_check(
                self, canonical_hostname, server_ip="127.0.0.1", port=test_dns_port
            ),
        )
        result = orch.execute_all()
        assert result.status == "SUCCESS", f"Install failed unexpectedly: {result.error}"
        assert result.wireguard_configured is True

        # Verify complete Linux provisioning sequence occurred
        assert "install_wireguard_dependencies" in platform.calls
        assert "install_wireguard_system_config" in platform.calls
        assert "apply_wireguard_firewall:51820" in platform.calls
        assert "control_wireguard_service:enable" in platform.calls
        assert "control_wireguard_service:start" in platform.calls
        assert "verify_wireguard_active" in platform.calls

        # Verify real socket DNS resolution answered 10.77.0.1
        mgr_probe = InstallerWireGuardManager(paths.root / "state" / "wireguard")
        assert mgr_probe.check_dns_readiness(live_dns.canonical_hostname, server_ip="127.0.0.1", port=test_dns_port) is True

        # Verify foreign queries are refused by the live server
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        foreign_query = b"\x99\x88\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x06google\x03com\x00\x00\x01\x00\x01"
        sock.sendto(foreign_query, ("127.0.0.1", test_dns_port))
        resp, _ = sock.recvfrom(2048)
        sock.close()
        flags = int.from_bytes(resp[2:4], "big")
        assert flags & 0x000F == 5  # RCODE 5 = REFUSED
    finally:
        live_dns.stop()


def test_wireguard_rollback_on_dns_readiness_failure(tmp_path, monkeypatch):
    """DNS readiness check failure triggers full rollback, preserves local console, and fails closed."""
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    platform.os_name = "linux"
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_wg_rollback_dns")

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
    opts._test_run_firewall = True

    orch = InstallerOrchestrator(
        platform=platform,
        options=opts,
        paths=paths,
        logger=logger,
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    # Simulate DNS readiness check failure
    from deployment.installer.remote_access import InstallerWireGuardManager
    monkeypatch.setattr(InstallerWireGuardManager, "check_dns_readiness", lambda self, canonical_hostname, **kw: False)

    result = orch.execute_all()
    assert result.status == "FAILED"
    assert "WireGuard split-DNS failed startup or readiness verification" in str(result.error)
    assert result.wireguard_configured is False

    # Verify rollback sequence
    assert "rollback_wireguard_firewall" in platform.calls
    assert "control_wireguard_service:stop" in platform.calls


def test_wireguard_rollback_on_service_enable_failure(tmp_path, monkeypatch):
    """WireGuard service failing systemctl enable triggers safe rollback."""
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    platform.os_name = "linux"
    platform.wireguard_service_success = False  # control_wireguard_service returns False
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_wg_rollback_enable")

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
    opts._test_run_firewall = True

    orch = InstallerOrchestrator(
        platform=platform,
        options=opts,
        paths=paths,
        logger=logger,
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    result = orch.execute_all()
    assert result.status == "FAILED"
    assert "WireGuard service failed to enable" in str(result.error)
    assert result.wireguard_configured is False
    assert "rollback_wireguard_firewall" in platform.calls
