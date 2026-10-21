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
from typing import Any, Callable, Dict, List, Optional, Tuple

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
        """Return paths that prove a previous or partial Chief installation exists.

        The current install creates source staging under
        ~/.local/share/chief/{tooling,release-source} and diagnostics under
        ~/.local/state/chief/installer before the existing-install scan. Those
        paths are deliberately *not* installation evidence. Runtime/profile
        markers, databases, release metadata and service definitions are.
        """
        candidates: List[Path] = []

        share_root = home / ".local" / "share" / "chief"
        share_markers = [
            share_root / "runtime" / "bin" / "python",
            share_root / "runtime" / "Scripts" / "python.exe",
            share_root / "managed",
            share_root / "production",
            share_root / "pwas",
            share_root / "caddy",
            share_root / "node",
        ]
        if any(p.exists() or p.is_symlink() for p in share_markers):
            candidates.append(share_root)

        state_root = home / ".local" / "state" / "chief"
        state_markers = [
            state_root / "release-metadata",
            state_root / "backups",
            state_root / "service-units",
        ]
        state_db_markers = list(state_root.glob("*.sqlite*")) + list(state_root.glob("*.db")) if state_root.exists() else []
        if any(p.exists() or p.is_symlink() for p in state_markers + state_db_markers):
            candidates.append(state_root)

        config_root = home / ".config" / "chief"
        config_markers = [
            config_root / "production-instance.json",
            config_root / "installation-identity.json",
            config_root / "browser-runtime.json",
            config_root / "Caddyfile",
        ]
        if any(p.exists() or p.is_symlink() for p in config_markers):
            candidates.append(config_root)

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

        # Archive only installed/runtime payloads. `tooling` and
        # `release-source` may have been freshly verified by this very run and
        # must stay in place for requirements installation and source manifest
        # generation.
        share_root = home / ".local" / "share" / "chief"
        for name in ("runtime", "managed", "production", "pwas", "caddy", "node", "browsers"):
            move_item(share_root / name, "share")

        # Preserve current installer diagnostics while moving previous release
        # metadata, backups, generated service units and database/state files.
        state_root = home / ".local" / "state" / "chief"
        for name in ("release-metadata", "backups", "service-units", "node-download", "caddy-download"):
            move_item(state_root / name, "state")
        if state_root.exists():
            for db_path in list(state_root.glob("*.sqlite*")) + list(state_root.glob("*.db")):
                move_item(db_path, "state")

        # Config is not populated by release validation, so any authoritative
        # config present at this point belongs to a previous/partial install.
        config_root = home / ".config" / "chief"
        if config_root.exists() or config_root.is_symlink():
            move_item(config_root, "config")

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
            "Nothing was deleted; previous/partial installed state was moved into this directory.\n"
            "Current-run verified source staging and installer diagnostics were left in place.\n"
            "Development/source directories outside authoritative Chief install state were left untouched.\n\n"
            + "\n".join(moved_records)
            + "\n"
        )
        (target_dir / "ARCHIVE_SUMMARY.txt").write_text(summary, encoding="utf-8")
        return target_dir

    def get_missing_system_packages(self) -> List[str]:
        from ..privileged.linux import _ALLOWED_PACKAGES
        missing: List[str] = []
        for pkg in sorted(_ALLOWED_PACKAGES):
            if pkg == "git" and shutil.which("git"):
                continue
            if pkg == "python3" and shutil.which("python3"):
                continue
            if pkg == "curl" and shutil.which("curl"):
                continue
            if pkg == "iproute2" and shutil.which("ip"):
                continue
            proc = subprocess.run(["dpkg", "-s", pkg], capture_output=True, text=True)
            if proc.returncode != 0 or "Status: install ok installed" not in proc.stdout:
                if pkg == "avahi-utils" and shutil.which("avahi-daemon"):
                    continue
                missing.append(pkg)
        return missing

    def install_system_dependencies(
        self,
        progress_callback: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        missing = self.get_missing_system_packages()
        if not missing:
            if progress_callback:
                progress_callback("All operating system packages already satisfied.", 1.0)
            return

        if progress_callback:
            progress_callback(f"Requesting administrator privilege for OS packages ({len(missing)} missing)...", 0.1)
        self._run_privileged(["install-packages", *missing])
        if progress_callback:
            progress_callback("Operating system packages installed successfully.", 1.0)

    def enable_user_linger(self, username: Optional[str] = None) -> None:
        user = username or self.get_current_username()
        if shutil.which("loginctl"):
            proc = subprocess.run(["loginctl", "show-user", user, "-p", "Linger"], capture_output=True, text=True)
            if proc.returncode == 0 and "Linger=yes" in proc.stdout:
                return
        self._run_privileged(["enable-linger", user])

    def configure_local_discovery(
        self,
        owner_name: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        import hashlib
        import unicodedata

        clean = " ".join(owner_name.strip().split())
        ascii_name = unicodedata.normalize("NFKD", clean).encode("ascii", "ignore").decode("ascii")
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
        if not slug:
            slug = "owner-" + hashlib.sha256(clean.encode("utf-8")).hexdigest()[:10]
        label = f"chief-{slug[:57].rstrip('-')}"

        if progress_callback:
            progress_callback(f"Configuring Avahi mDNS for {label}.local...")

        conf_path = Path("/etc/avahi/avahi-daemon.conf")
        already_configured = False
        if conf_path.exists():
            try:
                content = conf_path.read_text(encoding="utf-8")
                if f"host-name={label}" in content:
                    already_configured = True
            except Exception:
                pass

        if not already_configured:
            try:
                self._run_privileged(["configure-avahi", label])
            except PrivilegeError as exc:
                if progress_callback:
                    progress_callback(f"Notice: Avahi configuration skipped ({exc})")

        public_host = f"{label}.local"
        if progress_callback:
            progress_callback(f"Local mDNS hostname advertised: {public_host}")
        return public_host

    def set_executable_capability(self, executable: Path, capability: str) -> None:
        if capability != "cap_net_bind_service=+ep":
            raise PrivilegeError(f"Unsupported capability: {capability}")
        if shutil.which("getcap"):
            proc = subprocess.run(["getcap", str(executable)], capture_output=True, text=True)
            if proc.returncode == 0 and "cap_net_bind_service" in proc.stdout:
                return
        try:
            self._run_privileged(["set-caddy-capability", str(executable)])
        except PrivilegeError:
            pass

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

    def install_wireguard_dependencies(self, progress_callback: Optional[Callable[[str], None]] = None) -> None:
        needed = []
        if not shutil.which("wg"):
            needed.append("wireguard-tools")
        if not shutil.which("iptables"):
            needed.append("iptables")
        if not (Path("/sys/module/wireguard").exists() or shutil.which("wg")):
            needed.append("wireguard")

        if not needed:
            if progress_callback:
                progress_callback("WireGuard system tools already satisfied.")
            return

        if progress_callback:
            progress_callback(f"Installing WireGuard system dependencies ({', '.join(needed)})...")
        self._run_privileged(["install-packages", *needed])

    def install_wireguard_system_config(self, source_conf: Path) -> None:
        self._run_privileged(["install-wireguard-config", str(source_conf)])

    def control_wireguard_service(self, action: str) -> bool:
        try:
            self._run_privileged(["control-wireguard-service", action])
            return True
        except Exception:
            return False

    def apply_wireguard_firewall(self, port: int) -> None:
        self._run_privileged(["apply-wireguard-firewall", str(port)])

    def rollback_wireguard_firewall(self) -> None:
        try:
            self._run_privileged(["rollback-wireguard-firewall"])
        except Exception:
            pass

    def verify_wireguard_active(self) -> bool:
        if not shutil.which("systemctl") or not shutil.which("wg"):
            return False
        res_svc = subprocess.run(["systemctl", "is-active", "wg-quick@wg0"], capture_output=True, text=True)
        if res_svc.returncode != 0 or res_svc.stdout.strip() != "active":
            return False
        res_wg = subprocess.run(["wg", "show", "wg0"], capture_output=True, text=True)
        return res_wg.returncode == 0

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
            f"On-farm Chief console: https://{public_host}/\n"
            f"Remote Owner PWA:       https://{public_host}/owner/\n"
            f"Mobile Staff PWA:       https://{public_host}/staff/\n"
            f"Certificate: Chief-Local-CA.crt\n\n"
            "On iPhone/iPad:\n"
            "1. Transfer and open Chief-Local-CA.crt.\n"
            "2. Install the downloaded profile in Settings > General > VPN & Device Management.\n"
            "3. Go to Settings > General > About > Certificate Trust Settings.\n"
            "4. Enable full trust for the Chief local CA.\n"
            f"5. Open the remote Owner PWA (https://{public_host}/owner/) or the on-farm console (https://{public_host}/) in Safari.\n\n"
            "On Android:\n"
            "1. Open Settings > Security & Privacy > More Security Settings > Encryption & Credentials.\n"
            "2. Choose 'Install a certificate' > 'CA certificate'.\n"
            "3. Select Chief-Local-CA.crt.\n"
            f"4. Open the remote Owner PWA (https://{public_host}/owner/) or the on-farm console (https://{public_host}/) in Chrome.\n\n"
            "Only the public CA certificate is exported here. No private TLS key is copied.\n\n"
            "WireGuard Remote Access (When away from farm Wi-Fi):\n"
            "1. Install the WireGuard app from App Store (iOS) or Google Play (Android).\n"
            "2. Import the tunnel configuration 'Chief-Owner-WireGuard.conf'.\n"
            "3. Enable the tunnel. Chief will be reached securely at the exact same address:\n"
            f"   - On-farm console: https://{public_host}/\n"
            f"   - Remote Owner PWA: https://{public_host}/owner/\n"
            "4. On iOS/Android, configure 'On-Demand' to automatically activate WireGuard when leaving farm Wi-Fi.\n"
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

    def create_console_launchers(self, paths: Any, public_host: str) -> None:
        """Create distinct desktop entries for Chief Console and Chief Installer."""
        home = Path.home()
        apps_dir = home / ".local" / "share" / "applications"
        desktop_dir = home / "Desktop"
        apps_dir.mkdir(parents=True, exist_ok=True)

        exec_bin = "/opt/chief-installer/chief-installer"
        if not Path(exec_bin).exists():
            exec_bin = f"{sys.executable} -m deployment.installer"

        console_desktop = (
            "[Desktop Entry]\n"
            "Version=1.0\n"
            "Type=Application\n"
            "Name=Chief Console\n"
            "GenericName=Chief Farm Operations Console\n"
            "Comment=On-farm Chief Console desktop application\n"
            f"Exec={exec_bin} --console\n"
            "Icon=chief-console\n"
            "Terminal=false\n"
            "StartupNotify=true\n"
            "Categories=Office;System;\n"
            "StartupWMClass=ChiefConsole\n"
        )
        installer_desktop = (
            "[Desktop Entry]\n"
            "Version=1.0\n"
            "Type=Application\n"
            "Name=Chief Installer\n"
            "GenericName=Chief Installation Setup\n"
            "Comment=Install or repair Chief deployment\n"
            f"Exec={exec_bin} --gui\n"
            "Icon=system-software-install\n"
            "Terminal=false\n"
            "StartupNotify=true\n"
            "Categories=System;Setup;\n"
        )

        (apps_dir / "chief-console.desktop").write_text(console_desktop, encoding="utf-8")
        (apps_dir / "chief-installer.desktop").write_text(installer_desktop, encoding="utf-8")

        if desktop_dir.is_dir():
            dt_console = desktop_dir / "chief-console.desktop"
            dt_installer = desktop_dir / "chief-installer.desktop"
            dt_console.write_text(console_desktop, encoding="utf-8")
            dt_installer.write_text(installer_desktop, encoding="utf-8")
            try:
                dt_console.chmod(0o755)
                dt_installer.chmod(0o755)
            except OSError:
                pass

    def unregister_service_units(self, unit_files: List[Any]) -> None:
        """Stop, disable, and unregister systemd user service units."""
        names: List[str] = []
        for unit in unit_files:
            name = unit.name if isinstance(unit, Path) else str(unit)
            if not name.endswith(".service"):
                name = f"{name}.service"
            names.append(name)
            if isinstance(unit, Path) and unit.exists():
                try:
                    unit.unlink()
                except Exception:
                    pass

        if shutil.which("systemctl") and names:
            subprocess.run(["systemctl", "--user", "stop", *names], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["systemctl", "--user", "disable", *names], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["systemctl", "--user", "daemon-reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["systemctl", "--user", "reset-failed"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def terminate_service_processes(self, service_names: List[str]) -> None:
        """Terminate systemd user service processes."""
        if shutil.which("systemctl"):
            units = [s if s.endswith(".service") else f"{s}.service" for s in service_names]
            subprocess.run(["systemctl", "--user", "stop", *units], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def remove_console_launchers(self, paths: Any) -> List[str]:
        """Remove desktop launcher files for Chief Console and Chief Installer."""
        removed: List[str] = []
        home = Path.home()
        for p in [
            home / ".local" / "share" / "applications" / "chief-console.desktop",
            home / ".local" / "share" / "applications" / "chief-installer.desktop",
            home / "Desktop" / "chief-console.desktop",
            home / "Desktop" / "chief-installer.desktop",
        ]:
            if p.exists():
                try:
                    p.unlink()
                    removed.append(str(p))
                except Exception:
                    pass
        return removed

    def cleanup_local_discovery(self, public_host: str) -> bool:
        """Remove configured local discovery entries."""
        try:
            self._run_privileged(["cleanup-local-discovery", public_host])
            return True
        except PrivilegeError:
            raise
        except Exception as exc:
            # Fallback in unprivileged/mock unit test environments
            avahi_service = Path("/etc/avahi/services/chief.service")
            if avahi_service.exists():
                try:
                    avahi_service.unlink()
                except Exception:
                    return False
            return True

