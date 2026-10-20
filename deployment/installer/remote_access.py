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
    is_valid: bool
    cgnat_detected: bool
    is_private_ip: bool
    message: str
    router_guidance: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class InstallerWireGuardManager:
    """Configures WireGuard during Chief installation and prepares mobile peer profiles."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.server_key_path = self.state_dir / "server.key"
        self.wg_conf_path = self.state_dir / "wg0.conf"
        self.state_file = self.state_dir / "wireguard_config.json"

    def check_reachability(self, endpoint: str, port: int = 51820) -> ReachabilityReport:
        endpoint = endpoint.strip()
        port = int(port)
        is_ip = False
        parsed_ip: Optional[ipaddress.IPv4Address | ipaddress.IPv6Address] = None

        try:
            parsed_ip = ipaddress.ip_address(endpoint)
            is_ip = True
        except ValueError:
            pass

        if not is_ip:
            if not _HOSTNAME_RE.fullmatch(endpoint):
                return ReachabilityReport(
                    endpoint=endpoint,
                    port=port,
                    is_valid=False,
                    cgnat_detected=False,
                    is_private_ip=False,
                    message="Invalid endpoint format. Must be an IP address or DNS hostname.",
                    router_guidance="",
                )
            try:
                resolved = socket.gethostbyname(endpoint)
                parsed_ip = ipaddress.ip_address(resolved)
            except Exception as exc:
                return ReachabilityReport(
                    endpoint=endpoint,
                    port=port,
                    is_valid=False,
                    cgnat_detected=False,
                    is_private_ip=False,
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
                message=(
                    f"Endpoint {endpoint} is in Carrier-Grade NAT range (100.64.0.0/10). "
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
                message=(
                    f"Endpoint {endpoint} is a private local IP. "
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
            message="Endpoint is a routable public address.",
            router_guidance=f"Forward UDP port {port} on your farm router to the Chief server's local LAN IP.",
        )

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
