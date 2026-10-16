"""Linux operating-system adapter for Chief Unified Installer.

Supports Xubuntu (reference target), Ubuntu, and Kali without separate installer codebases.
"""
from datetime import datetime, timezone
import os
from pathlib import Path
import platform as py_platform
import re
import shutil
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

from ..errors import PrivilegeError, SystemDependencyError
from ..models import PlatformInfo, PreflightCheck, PreflightReport
from .base import PlatformAdapter

_FROZEN_EXECUTABLE_ENV = "CHIEF_INSTALLER_FROZEN_EXECUTABLE"


class LinuxPlatformAdapter(PlatformAdapter):
    """Unified Linux platform adapter for Debian-family distributions."""

    def __init__(self, helper_executable: Optional[List[str]] = None):
        self._helper_cmd = helper_executable

    def _get_helper_prefix(self) -> List[str]:
        if self._helper_cmd:
            return list(self._helper_cmd)

        helper_module = "deployment.installer.privileged.linux"

        # In a PyInstaller build sys.executable normally points at the frozen
        # Chief Installer binary, which cannot be invoked as `python -m ...`.
        # Re-enter the packaged executable through its constrained internal
        # helper mode instead. Source/zipapp execution keeps the module path.
        if getattr(sys, "frozen", False):
            frozen_executable = os.environ.get(_FROZEN_EXECUTABLE_ENV)
            if not frozen_executable:
                frozen_executable = str(Path(sys.argv[0]).resolve())
            helper_cmd = [frozen_executable, "--internal-privileged-helper"]
        else:
            python_exe = sys.executable or shutil.which("python3") or shutil.which("python") or "python3"
            helper_cmd = [python_exe, "-m", helper_module]

        if shutil.which("pkexec"):
            return ["pkexec", *helper_cmd]
        elif shutil.which("sudo"):
            return ["sudo", *helper_cmd]
        else:
            return helper_cmd

    def _run_privileged(self, args: List[str]) -> None:
        cmd = self._get_helper_prefix() + args
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                err_msg = proc.stderr.strip() or proc.stdout.strip() or f"Process exited with {proc.returncode}"
                raise PrivilegeError(f"Privileged operation failed: {err_msg}", details={"command": args, "error": err_msg})
        except FileNotFoundError as exc:
            raise PrivilegeError(f"Privilege elevation helper not found: {exc}") from exc

    def get_current_username(self) -> str:
        import getpass
        return os.environ.get("USER") or os.environ.get("LOGNAME") or getpass.getuser()

    def get_platform_info(self) -> PlatformInfo:
        distro_id = "linux"
        distro_name = "Linux"
        distro_version = ""
        details: Dict[str, str] = {}

        os_release = Path("/etc/os-release")
        if os_release.exists():
            try:
                for line in os_release.read_text(encoding="utf-8").splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        clean_v = v.strip().strip('"').strip("'")
                        details[k.strip()] = clean_v
                distro_id = details.get("ID", distro_id).lower()
                distro_name = details.get("NAME", distro_name)
                distro_version = details.get("VERSION_ID", "")
            except Exception:
                pass

        arch = py_platform.machine().lower()
        id_like = details.get("ID_LIKE", "").lower()
        is_debian_family = (
            distro_id in {"xubuntu", "ubuntu", "kali", "debian", "linuxmint", "pop"}
            or "debian" in id_like
            or "ubuntu" in id_like
        )
        is_supported = is_debian_family and arch in {"x86_64", "amd64", "aarch64", "arm64"}

        return PlatformInfo(
            os_name="linux",
            distro_id=distro_id,
            distro_name=distro_name,
            distro_version=distro_version,
            architecture=arch,
            is_supported=is_supported,
            details=details,
        )

    def run_preflight_checks(self) -> PreflightReport:
        info = self.get_platform_info()
        checks: List[PreflightCheck] = []

        # 1. OS check
        is_linux = py_platform.system() == "Linux"
        checks.append(PreflightCheck(
            name="os_linux",
            passed=is_linux,
            message="Operating system is Linux" if is_linux else f"Non-Linux system detected: {py_platform.system()}",
            required=True,
        ))

        # 2. Supported distro check
        checks.append(PreflightCheck(
            name="distribution_supported",
            passed=info.is_supported,
            message=f"Distribution {info.distro_name} ({info.distro_id}) is supported" if info.is_supported else f"Unsupported distribution: {info.distro_name}",
            required=True,
        ))

        # 3. Disk space check (>= 5 GB free)
        free_bytes = shutil.disk_usage(Path.home()).free
        free_gb = round(free_bytes / (1024**3), 2)
        has_disk = free_gb >= 5.0
        checks.append(PreflightCheck(
            name="disk_space",
            passed=has_disk,
            message=f"{free_gb} GB disk space available" if has_disk else f"Insufficient disk space: {free_gb} GB free, minimum 5 GB required",
            required=True,
        ))

        # 4. Architecture check
        arch_ok = info.architecture in {"x86_64", "amd64", "aarch64", "arm64"}
        checks.append(PreflightCheck(
            name="cpu_architecture",
            passed=arch_ok,
            message=f"CPU architecture {info.architecture} supported" if arch_ok else f"Unsupported CPU architecture: {info.architecture}",
            required=True,
        ))

        # 5. Apt package manager check
        has_apt = bool(shutil.which("apt-get"))
        checks.append(PreflightCheck(
            name="package_manager_apt",
            passed=has_apt,
            message="apt-get package manager available" if has_apt else "apt-get is required on Debian/Ubuntu systems",
            required=True,
        ))

        # 6. Systemd user session check
        has_systemctl = bool(shutil.which("systemctl"))
        checks.append(PreflightCheck(
            name="systemd_available",
            passed=has_systemctl,
            message="systemd service manager available" if has_systemctl else "systemctl is required for Chief user services",
            required=True,
        ))

        # 7. Bootstrap Python check. The native installer launches by itself,
        # but the pinned Chief source contract and isolated venv require a real
        # host Python interpreter; a frozen executable must never impersonate it.
        bootstrap_python = shutil.which("python3") or shutil.which("python")
        checks.append(PreflightCheck(
            name="python_bootstrap",
            passed=bool(bootstrap_python),
            message=f"Bootstrap Python available at {bootstrap_python}" if bootstrap_python else "python3 is required to create Chief's isolated runtime",
            required=True,
        ))

        overall_passed = all(c.passed for c in checks if c.required)
        return PreflightReport(platform=info, checks=checks, passed=overall_passed)

    def _installed_state_candidates(self, home: Path) -> List[Path]:
        """Return authoritative Chief installation/profile state paths only.

        Generic developer/source directories such as ~/chief-agent,
        ~/chief-runtime and ~/chief-release* are intentionally excluded. They
        are not proof of an installed Chief instance and must never be moved by
        the production installer merely because they exist.
        """
        candidates: List[Path] = [
            home / ".local" / "share" / "chief",
            home / ".local" / "state" / "chief",
            home / ".config" / "chief",
        ]

        unit_dir = home / ".config" / "systemd" / "user"
        if unit_dir.exists():
            candidates.extend(unit_dir.glob("chief-*.service"))
            candidates.extend(unit_dir.glob("chief-*.timer"))
            candidates.extend(unit_dir.glob("chief-*.service.d"))
            wants_dir = unit_dir / "default.target.wants"
            if wants_dir.exists():
                candidates.extend(wants_dir.glob("chief-*"))

        return list(candidates)

    def detect_existing_installation(self) -> Tuple[bool, List[str]]:
        home = Path.home()
        existing = [
            str(p)
            for p in self._installed_state_candidates(home)
            if p.exists() or p.is_symlink()
        ]
        return (len(existing) > 0, existing)

    def archive_existing_installation(
        self,
        archive_root: Path,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        target_dir = archive_root / stamp
        subdirs = ["home", "state", "share", "config", "systemd", "system", "logs", "repositories", "inventory"]
        for s in subdirs:
            (target_dir / s).mkdir(parents=True, exist_ok=True, mode=0o700)

        service_names = [
            "chief-agent-dashboard.service",
            "chief-agent-worker.service",
            "chief-agent-scheduler.service",
            "chief-tls-proxy.service",
            "chief-n8n-container.service",
            "chief-n8n.service",
        ]

        # Stop and disable old user services before moving their definitions.
        self.control_services("stop", service_names)
        self.control_services("disable", service_names)

        moved_records: List[str] = []
        home = Path.home()

        def move_item(src: Path, bucket: str) -> None:
            if not (src.exists() or src.is_symlink()):
                return
            if archive_root in src.parents or src == archive_root:
                return
            dest = target_dir / bucket / src.name
            if dest.exists() or dest.is_symlink():
                dest = target_dir / bucket / f"{src.name}__{stamp}"
            if progress_callback:
                progress_callback(f"Archiving: {src}")
            try:
                shutil.move(str(src), str(dest))
                moved_records.append(f"{src}\t{dest}")
            except Exception as exc:
                if progress_callback:
                    progress_callback(f"Warning: could not move {src}: {exc}")

        # Archive authoritative XDG installation/profile state only. Development
        # source trees under ~/chief-* are deliberately preserved.
        move_item(home / ".local" / "share" / "chief", "share")
        move_item(home / ".local" / "state" / "chief", "state")
        move_item(home / ".config" / "chief", "config")

        # Archive user systemd units, timers, drop-ins and activation links.
        unit_dir = home / ".config" / "systemd" / "user"
        if unit_dir.exists():
            for unit in list(unit_dir.glob("chief-*.service")):
                move_item(unit, "systemd")
            for timer in list(unit_dir.glob("chief-*.timer")):
                move_item(timer, "systemd")
            for dropin in list(unit_dir.glob("chief-*.service.d")):
                move_item(dropin, "systemd")
            wants_dir = unit_dir / "default.target.wants"
            if wants_dir.exists():
                for link in list(wants_dir.glob("chief-*")):
                    move_item(link, "systemd")

        self.reload_service_manager()

        # Write summary
        summary = (
            f"Archived at {datetime.now(timezone.utc).isoformat()}\n"
            "Nothing was deleted; all items were moved into this directory.\n"
            "Development/source directories outside authoritative Chief install state were left untouched.\n\n"
            + "\n".join(moved_records)
            + "\n"
        )
        (target_dir / "ARCHIVE_SUMMARY.txt").write_text(summary, encoding="utf-8")
        return target_dir

    def install_system_dependencies(
        self,
        progress_callback: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        if progress_callback:
            progress_callback("Requesting administrator privilege for OS packages...", 0.1)
        self._run_privileged(["install-packages"])
        if progress_callback:
            progress_callback("Operating system packages installed successfully.", 1.0)

    def enable_user_linger(self, username: Optional[str] = None) -> None:
        user = username or os.environ.get("USER") or py_platform.node()
        self._run_privileged(["enable-linger", user])

    def configure_local_discovery(
        self,
        owner_name: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        from ..installation_identity import local_host_label

        base_label = local_host_label(owner_name)
        label = base_label
        if progress_callback:
            progress_callback(f"Configuring Avahi mDNS for {label}.local...")

        self._run_privileged(["configure-avahi", label])

        # Verify resolution
        public_host = f"{label}.local"
        if progress_callback:
            progress_callback(f"Local mDNS hostname advertised: {public_host}")
        return public_host

    def set_executable_capability(self, executable: Path, capability: str) -> None:
        if capability != "cap_net_bind_service=+ep":
            raise PrivilegeError(f"Unsupported capability: {capability}")
        self._run_privileged(["set-caddy-capability", str(executable)])

    def install_service_units(self, stage_dir: Path, dest_dir: Path) -> None:
        dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for unit_file in stage_dir.glob("*.service"):
            target = dest_dir / unit_file.name
            shutil.copy2(unit_file, target)
            target.chmod(0o600)

    def reload_service_manager(self) -> None:
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "daemon-reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def control_services(self, action: str, service_names: List[str]) -> Dict[str, bool]:
        if action not in {"start", "stop", "restart", "enable", "disable"}:
            raise ValueError(f"Invalid service action: {action}")
        if not shutil.which("systemctl"):
            return {s: False for s in service_names}
        cmd = ["systemctl", "--user", action] + service_names
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {s: proc.returncode == 0 for s in service_names}

    def verify_service_active(self, service_name: str) -> bool:
        if not shutil.which("systemctl"):
            return False
        proc = subprocess.run(["systemctl", "--user", "is-active", "--quiet", service_name])
        return proc.returncode == 0

    def export_device_ca(
        self,
        ca_cert_path: Path,
        destination_dir: Path,
        owner_name: str,
        public_host: str,
    ) -> Path:
        destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target_cert = destination_dir / "Chief-Local-CA.crt"
        if ca_cert_path.exists():
            shutil.copy2(ca_cert_path, target_cert)
            target_cert.chmod(0o644)

        readme_content = (
            f"Chief device setup for {owner_name}\n\n"
            f"Chief address: https://{public_host}/owner/\n"
            f"Certificate: Chief-Local-CA.crt\n\n"
            "On iPhone/iPad:\n"
            "1. Transfer and open Chief-Local-CA.crt.\n"
            "2. Install the downloaded profile in Settings > General > VPN & Device Management.\n"
            "3. Go to Settings > General > About > Certificate Trust Settings.\n"
            "4. Enable full trust for the Chief local CA.\n"
            "5. Open the Chief address above in Safari.\n\n"
            "On Android:\n"
            "1. Open Settings > Security & Privacy > More Security Settings > Encryption & Credentials.\n"
            "2. Choose 'Install a certificate' > 'CA certificate'.\n"
            "3. Select Chief-Local-CA.crt.\n"
            "4. Open the Chief address above in Chrome.\n\n"
            "Only the public CA certificate is exported here. No private TLS key is copied.\n"
        )
        readme = destination_dir / "README.txt"
        readme.write_text(readme_content, encoding="utf-8")
        readme.chmod(0o600)
        return readme

    def open_url_in_browser(self, url: str) -> bool:
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        import webbrowser
        return webbrowser.open(url)
