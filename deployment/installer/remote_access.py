"""WireGuard remote-access helper and peer provisioning for Chief Unified Installer."""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

# RFC 6598 Carrier-Grade NAT range: 100.64.0.0/10
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
]
_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$")


def generate_wireguard_keypair() -> Tuple[str, str]:
    """Generate a WireGuard-compatible Curve25519 keypair."""
    private_key = x25519.X25519PrivateKey.generate()
    public_key = private_key.public_key()
    priv_raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return (
        base64.b64encode(priv_raw).decode("ascii"),
        base64.b64encode(pub_raw).decode("ascii"),
    )


def generate_preshared_key() -> str:
    """Generate a random 32-byte WireGuard preshared key."""
    return base64.b64encode(os.urandom(32)).decode("ascii")


@dataclass
class ReachabilityReport:
    endpoint: str
    port: int
    is_valid: bool = False
    cgnat_detected: bool = False
    is_private_ip: bool = False
    syntax_valid: bool = False
    dns_resolved: bool = False
    is_public: bool = False
    udp_verified: bool = False
    reachability_status: str = "UNKNOWN"
    message: str = ""
    router_guidance: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class InstallerWireGuardManager:
    """Configures WireGuard during Chief installation and prepares mobile peer profiles."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.server_key_path = self.state_dir / "server.key"
        self.wg_conf_path = self.state_dir / "wg0.conf"
        self.state_file = self.state_dir / "wireguard_config.json"
        self._firewall_applied = False
        self._service_started = False

    def check_reachability(self, endpoint: str, port: int = 51820) -> ReachabilityReport:
        endpoint = (endpoint or "").strip()
        port = int(port)
        is_ip = False
        parsed_ip: Optional[ipaddress.IPv4Address | ipaddress.IPv6Address] = None

        try:
            parsed_ip = ipaddress.ip_address(endpoint)
            is_ip = True
        except ValueError:
            pass

        syntax_valid = False
        dns_resolved = False

        if is_ip:
            syntax_valid = True
            dns_resolved = True
        else:
            if not _HOSTNAME_RE.fullmatch(endpoint):
                return ReachabilityReport(
                    endpoint=endpoint,
                    port=port,
                    is_valid=False,
                    cgnat_detected=False,
                    is_private_ip=False,
                    syntax_valid=False,
                    dns_resolved=False,
                    is_public=False,
                    udp_verified=False,
                    reachability_status="SYNTAX_INVALID",
                    message="Invalid endpoint format. Must be an IP address or DNS hostname.",
                    router_guidance="",
                )
            syntax_valid = True
            try:
                resolved = socket.gethostbyname(endpoint)
                parsed_ip = ipaddress.ip_address(resolved)
                dns_resolved = True
            except Exception as exc:
                return ReachabilityReport(
                    endpoint=endpoint,
                    port=port,
                    is_valid=False,
                    cgnat_detected=False,
                    is_private_ip=False,
                    syntax_valid=True,
                    dns_resolved=False,
                    is_public=False,
                    udp_verified=False,
                    reachability_status="DNS_RESOLUTION_FAILED",
                    message=f"DNS lookup failed for {endpoint}: {exc}",
                    router_guidance="Check your dynamic DNS or domain provider settings.",
                )

        if parsed_ip in _CGNAT_NET:
            return ReachabilityReport(
                endpoint=endpoint,
                port=port,
                is_valid=False,
                cgnat_detected=True,
                is_private_ip=False,
                syntax_valid=syntax_valid,
                dns_resolved=dns_resolved,
                is_public=False,
                udp_verified=False,
                reachability_status="CGNAT_BLOCKED",
                message=(
                    f"Endpoint {endpoint} resolves to Carrier-Grade NAT range ({parsed_ip} in 100.64.0.0/10). "
                    "Direct inbound WireGuard connections cannot reach this farm connection without a public IP from your ISP."
                ),
                router_guidance="Contact your Internet Service Provider to request a routable public IPv4 address.",
            )

        if any(parsed_ip in net for net in _PRIVATE_NETS):
            return ReachabilityReport(
                endpoint=endpoint,
                port=port,
                is_valid=False,
                cgnat_detected=False,
                is_private_ip=True,
                syntax_valid=syntax_valid,
                dns_resolved=dns_resolved,
                is_public=False,
                udp_verified=False,
                reachability_status="PRIVATE_WAN_BLOCKED",
                message=(
                    f"Endpoint {endpoint} resolves to private IP address {parsed_ip}. "
                    "Remote clients away from the farm cannot connect over the internet to a private address."
                ),
                router_guidance="Enter your public WAN IP or a Dynamic DNS hostname.",
            )

        return ReachabilityReport(
            endpoint=endpoint,
            port=port,
            is_valid=True,
            cgnat_detected=False,
            is_private_ip=False,
            syntax_valid=syntax_valid,
            dns_resolved=dns_resolved,
            is_public=True,
            udp_verified=False,
            reachability_status="PUBLIC_UNVERIFIED",
            message=f"Endpoint {endpoint} ({parsed_ip}) has valid syntax and public routing, but UDP reachability cannot be assumed without a live client handshake.",
            router_guidance=f"Forward UDP port {port} on your farm router to the Chief server's local LAN IP.",
        )

    def apply_firewall_policy(self, port: int = 51820) -> None:
        """Apply fail-closed atomic firewall policy using dedicated chains."""
        iptables = shutil.which("iptables")
        if not iptables:
            return

        def _run(cmd: List[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(cmd, capture_output=True, text=True, check=False)

        # Create chains if needed
        _run([iptables, "-N", "CHIEF-WG-IN"])
        _run([iptables, "-F", "CHIEF-WG-IN"])
        _run([iptables, "-N", "CHIEF-WG-FWD"])
        _run([iptables, "-F", "CHIEF-WG-FWD"])

        # Insert jumps if not already present
        chk_in = _run([iptables, "-C", "INPUT", "-j", "CHIEF-WG-IN"])
        if chk_in.returncode != 0:
            _run([iptables, "-I", "INPUT", "1", "-j", "CHIEF-WG-IN"])

        chk_fwd = _run([iptables, "-C", "FORWARD", "-j", "CHIEF-WG-FWD"])
        if chk_fwd.returncode != 0:
            _run([iptables, "-I", "FORWARD", "1", "-j", "CHIEF-WG-FWD"])

        # Allow inbound UDP for WireGuard handshake
        _run([iptables, "-A", "CHIEF-WG-IN", "-p", "udp", "--dport", str(port), "-j", "ACCEPT"])

        # Allow HTTPS (443) and authoritative DNS (53) strictly over wg0
        _run([iptables, "-A", "CHIEF-WG-IN", "-i", "wg0", "-p", "tcp", "--dport", "443", "-j", "ACCEPT"])
        _run([iptables, "-A", "CHIEF-WG-IN", "-i", "wg0", "-p", "udp", "--dport", "53", "-j", "ACCEPT"])
        _run([iptables, "-A", "CHIEF-WG-IN", "-i", "wg0", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"])

        # Fail-closed: drop all other traffic arriving on wg0
        _run([iptables, "-A", "CHIEF-WG-IN", "-i", "wg0", "-j", "DROP"])

        # Fail-closed forwarding: prevent WireGuard peers from routing into private farm LAN
        _run([iptables, "-A", "CHIEF-WG-FWD", "-i", "wg0", "-d", "192.168.0.0/16", "-j", "DROP"])
        _run([iptables, "-A", "CHIEF-WG-FWD", "-i", "wg0", "-d", "10.0.0.0/8", "-j", "DROP"])
        _run([iptables, "-A", "CHIEF-WG-FWD", "-i", "wg0", "-d", "172.16.0.0/12", "-j", "DROP"])
        _run([iptables, "-A", "CHIEF-WG-FWD", "-i", "wg0", "-j", "DROP"])

        self._firewall_applied = True

    def rollback_firewall_policy(self) -> None:
        """Remove dedicated firewall chains without clobbering host rules."""
        iptables = shutil.which("iptables")
        if not iptables:
            return

        def _run(cmd: List[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(cmd, capture_output=True, text=True, check=False)

        _run([iptables, "-D", "INPUT", "-j", "CHIEF-WG-IN"])
        _run([iptables, "-D", "FORWARD", "-j", "CHIEF-WG-FWD"])
        _run([iptables, "-F", "CHIEF-WG-IN"])
        _run([iptables, "-X", "CHIEF-WG-IN"])
        _run([iptables, "-F", "CHIEF-WG-FWD"])
        _run([iptables, "-X", "CHIEF-WG-FWD"])
        self._firewall_applied = False

    def enable_and_start_service(self) -> None:
        """Enable and start the wg-quick systemd service on Linux."""
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return
        res_enable = subprocess.run([systemctl, "enable", "wg-quick@wg0"], capture_output=True, text=True, check=False)
        res_start = subprocess.run([systemctl, "start", "wg-quick@wg0"], capture_output=True, text=True, check=False)
        if res_start.returncode != 0:
            raise RuntimeError(f"Failed to start wg-quick@wg0 service: {res_start.stderr.strip()}")
        self._service_started = True

    def stop_service(self) -> None:
        """Stop the wg-quick systemd service."""
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return
        subprocess.run([systemctl, "stop", "wg-quick@wg0"], capture_output=True, text=True, check=False)
        self._service_started = False

    def rollback(self) -> None:
        """Safely clean up WireGuard server resources without affecting local Chief services."""
        if self._service_started:
            try:
                self.stop_service()
            except Exception:
                pass
        if self._firewall_applied:
            try:
                self.rollback_firewall_policy()
            except Exception:
                pass

    def provision_initial_server_and_owner(
        self,
        endpoint: str,
        canonical_hostname: str,
        owner_name: str,
        port: int = 51820,
        vpn_subnet: str = "10.77.0.0/24",
    ) -> Tuple[str, str]:
        """Provisions initial WireGuard server and Owner peer profile.

        Returns:
            (server_wg_conf_content, owner_client_conf_content)
        """
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        vpn_net = ipaddress.ip_network(vpn_subnet, strict=False)
        hosts = list(vpn_net.hosts())
        server_ip = str(hosts[0])
        owner_ip = str(hosts[1])

        # Server keys
        server_priv, server_pub = generate_wireguard_keypair()
        self.server_key_path.write_text(server_priv, encoding="utf-8")
        try:
            self.server_key_path.chmod(0o600)
        except Exception:
            pass

        # Owner peer keys
        owner_priv, owner_pub = generate_wireguard_keypair()
        psk = generate_preshared_key()

        server_conf = (
            "# Chief Authoritative WireGuard Configuration\n"
            "# Fail-closed access to Chief HTTPS\n"
            "[Interface]\n"
            f"Address = {server_ip}/32\n"
            f"ListenPort = {port}\n"
            f"PrivateKey = {server_priv}\n\n"
            f"# Owner Peer: {owner_name}\n"
            "[Peer]\n"
            f"PublicKey = {owner_pub}\n"
            f"PresharedKey = {psk}\n"
            f"AllowedIPs = {owner_ip}/32\n"
        )
        self.wg_conf_path.write_text(server_conf, encoding="utf-8")
        try:
            self.wg_conf_path.chmod(0o600)
        except Exception:
            pass

        client_conf = (
            f"# Chief Remote Access: {owner_name} (Owner)\n"
            f"# Host: {canonical_hostname}\n"
            "[Interface]\n"
            f"PrivateKey = {owner_priv}\n"
            f"Address = {owner_ip}/32\n"
            f"DNS = {server_ip}\n\n"
            "[Peer]\n"
            f"PublicKey = {server_pub}\n"
            f"PresharedKey = {psk}\n"
            f"Endpoint = {endpoint}:{port}\n"
            f"AllowedIPs = {server_ip}/32\n"
            "PersistentKeepalive = 25\n"
        )

        state_data = {
            "enabled": True,
            "endpoint": endpoint,
            "port": port,
            "vpn_subnet": vpn_subnet,
            "server_vpn_ip": server_ip,
            "server_public_key": server_pub,
            "canonical_hostname": canonical_hostname,
            "peers": {
                "owner-001": {
                    "name": owner_name,
                    "role": "Owner",
                    "vpn_ip": owner_ip,
                    "public_key": owner_pub,
                    "enabled": True,
                }
            },
        }
        self.state_file.write_text(json.dumps(state_data, indent=2), encoding="utf-8")
        try:
            self.state_file.chmod(0o600)
        except Exception:
            pass

        return server_conf, client_conf
