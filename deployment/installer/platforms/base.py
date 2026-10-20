"""Abstract platform adapter interface for Chief Unified Installer."""
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..models import PlatformInfo, PreflightReport


class PlatformAdapter(ABC):
    """Abstract operating system adapter.

    The shared installer core calls into PlatformAdapter for OS-specific tasks.
    OS adapters contain zero GUI logic and zero business decision logic.
    """

    @abstractmethod
    def get_platform_info(self) -> PlatformInfo:
        """Detect OS, distribution family, release, and architecture."""
        raise NotImplementedError

    @abstractmethod
    def get_current_username(self) -> str:
        """Return operating-system user account executing the installation."""
        raise NotImplementedError

    @abstractmethod
    def run_preflight_checks(self) -> PreflightReport:
        """Check host capabilities (disk space, network, permissions, dependencies)."""
        raise NotImplementedError

    @abstractmethod
    def detect_existing_installation(self) -> Tuple[bool, List[str]]:
        """Detect any existing Chief installations or legacy runtime paths."""
        raise NotImplementedError

    @abstractmethod
    def archive_existing_installation(
        self,
        archive_root: Path,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Path:
        """Move existing Chief paths into timestamped archive directory safely."""
        raise NotImplementedError

    @abstractmethod
    def install_system_dependencies(
        self,
        progress_callback: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        """Install required OS packages through the platform package manager."""
        raise NotImplementedError

    @abstractmethod
    def enable_user_linger(self, username: Optional[str] = None) -> None:
        """Enable persistent user services across logouts if required by OS."""
        raise NotImplementedError

    @abstractmethod
    def configure_local_discovery(
        self,
        owner_name: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Configure local mDNS hostname (e.g. via Avahi on Linux) and return selected public_host."""
        raise NotImplementedError

    @abstractmethod
    def set_executable_capability(self, executable: Path, capability: str) -> None:
        """Grant low-port binding or executable capability without running full process as root."""
        raise NotImplementedError

    @abstractmethod
    def install_service_units(self, stage_dir: Path, dest_dir: Path) -> None:
        """Install generated service definitions into the appropriate user/system location."""
        raise NotImplementedError

    @abstractmethod
    def reload_service_manager(self) -> None:
        """Reload systemd or platform init daemon."""
        raise NotImplementedError

    @abstractmethod
    def control_services(self, action: str, service_names: List[str]) -> Dict[str, bool]:
        """Start, stop, enable, or restart user background services."""
        raise NotImplementedError

    @abstractmethod
    def verify_service_active(self, service_name: str) -> bool:
        """Check whether a background service is actively running."""
        raise NotImplementedError

    @abstractmethod
    def export_device_ca(
        self,
        ca_cert_path: Path,
        destination_dir: Path,
        owner_name: str,
        public_host: str,
    ) -> Path:
        """Export local TLS root CA certificate and mobile instructions for phone pairing."""
        raise NotImplementedError

    @abstractmethod
    def open_url_in_browser(self, url: str) -> bool:
        """Launch URL in the user's default web browser."""
        raise NotImplementedError

    def install_wireguard_dependencies(self, progress_callback: Optional[Callable[[str], None]] = None) -> None:
        """Install WireGuard server tools and dependencies."""
        pass

    def install_wireguard_system_config(self, source_conf: Path) -> None:
        """Install wg0.conf to /etc/wireguard/wg0.conf."""
        raise NotImplementedError

    def control_wireguard_service(self, action: str) -> bool:
        """Control wg-quick systemd service."""
        raise NotImplementedError

    def apply_wireguard_firewall(self, port: int) -> None:
        """Apply fail-closed WireGuard firewall rules."""
        raise NotImplementedError

    def rollback_wireguard_firewall(self) -> None:
        """Remove WireGuard firewall rules."""
        pass

    def verify_wireguard_active(self) -> bool:
        """Verify WireGuard interface and service are active."""
        return False

    def probe_health_endpoint(self, url: str, timeout: float = 2.0, headers: Optional[Dict[str, str]] = None) -> bool:
        """Check whether the application health endpoint responds with 200 OK."""
        import urllib.request
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False
