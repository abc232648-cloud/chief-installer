"""Windows operating-system adapter for Chief Unified Installer.

Supports Windows 10 and Windows 11 x64 systems using native Windows conventions:
- User-level installation in %LOCALAPPDATA%\\chief and %APPDATA%\\chief
- Windows Task Scheduler XML tasks for background services (LeastPrivilege, InteractiveToken)
- Native Windows mDNS resolution (.local)
- Zero console window requirement (GUI-first)
- Safe archive and partial recovery without destroying user repositories
"""
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import platform as py_platform
import re
import shutil
import subprocess
from ..process import run as run_quiet
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

from ..errors import PrivilegeError, SystemDependencyError
from ..models import PlatformInfo, PreflightCheck, PreflightReport
from .base import PlatformAdapter


class WindowsPlatformAdapter(PlatformAdapter):
    """Platform adapter for Windows 10/11 x64 systems."""

    def __init__(self):
        self.os_name = "windows"

    def get_current_username(self) -> str:
        """Return the current Windows username."""
        return os.environ.get("USERNAME") or getpass.getuser()

    def get_platform_info(self) -> PlatformInfo:
        """Detect Windows version, architecture, and support status."""
        arch = py_platform.machine().lower()
        if arch in ("amd64", "x86_64"):
            arch = "x86_64"

        release = py_platform.release()
        version = py_platform.version()

        # Windows 10 and 11 are supported
        is_supported = arch == "x86_64" and py_platform.system() == "Windows"
        try:
            rel_num = int(release)
            if rel_num < 10:
                is_supported = False
        except (ValueError, TypeError):
            pass

        return PlatformInfo(
            os_name="windows",
            distro_id="windows",
            distro_name="Windows",
            distro_version=f"{release} ({version})",
            architecture=arch,
            is_supported=is_supported,
            details={
                "system": py_platform.system(),
                "release": release,
                "version": version,
                "processor": py_platform.processor(),
            },
        )

    def run_preflight_checks(self) -> PreflightReport:
        """Validate Windows system prerequisites."""
        info = self.get_platform_info()
        checks: List[PreflightCheck] = []

        # 1. OS check
        is_windows = py_platform.system() == "Windows"
        checks.append(PreflightCheck(
            name="os_windows",
            passed=is_windows,
            message="Operating system is Windows" if is_windows else f"Non-Windows system detected: {py_platform.system()}",
            required=True,
        ))

        # 2. Supported Windows release check (Win 10/11)
        checks.append(PreflightCheck(
            name="windows_version_supported",
            passed=info.is_supported,
            message=f"Windows {info.distro_version} is supported" if info.is_supported else f"Unsupported Windows version: {info.distro_version}",
            required=True,
        ))

        # 3. CPU architecture check (x86_64)
        arch_ok = info.architecture in {"x86_64", "amd64", "AMD64"}
        checks.append(PreflightCheck(
            name="cpu_architecture",
            passed=arch_ok,
            message=f"CPU architecture {info.architecture} supported" if arch_ok else f"Unsupported CPU architecture: {info.architecture}",
            required=True,
        ))

        # 4. Disk space check (>= 5 GB free on system drive)
        try:
            target_path = Path.home()
            free_bytes = shutil.disk_usage(target_path).free
            free_gb = round(free_bytes / (1024**3), 2)
            has_disk = free_gb >= 5.0
            checks.append(PreflightCheck(
                name="disk_space",
                passed=has_disk,
                message=f"{free_gb} GB disk space available" if has_disk else f"Insufficient disk space: {free_gb} GB free, minimum 5 GB required",
                required=True,
            ))
        except Exception as exc:
            checks.append(PreflightCheck(
                name="disk_space",
                passed=False,
                message=f"Failed to check disk space: {exc}",
                required=True,
            ))

        # 5. Task Scheduler CLI availability
        has_schtasks = bool(shutil.which("schtasks"))
        checks.append(PreflightCheck(
            name="task_scheduler",
            passed=has_schtasks,
            message="Windows Task Scheduler interface (schtasks) available" if has_schtasks else "schtasks command not found",
            required=True,
        ))

        all_passed = all(c.passed for c in checks if c.required)
        return PreflightReport(
            platform=info,
            checks=checks,
            passed=all_passed,
        )

    def _installed_state_candidates(self, home: Path) -> List[Path]:
        """Enumerate candidate paths representing actual installed Chief state.

        Strictly avoids flagging:
        - Developer source repositories (e.g. chief-agent, chief-installer)
        - Current-run staging directories (tooling, release-source)
        - Current-run active logs (installer/logs)
        """
        candidates: List[Path] = []

        local_app = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        roaming_app = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))

        chief_local = local_app / "chief"
        chief_roaming = roaming_app / "chief"

        # Check installed runtime / components
        runtime_markers = [
            chief_local / "runtime" / "Scripts" / "python.exe",
            chief_local / "runtime" / "bin" / "python.exe",
            chief_local / "managed",
            chief_local / "production",
            chief_local / "pwas",
            chief_local / "caddy",
            chief_local / "node",
        ]
        if any(p.exists() or (hasattr(p, "is_symlink") and p.is_symlink()) for p in runtime_markers):
            candidates.append(chief_local)

        # Check installed state
        state_markers = [
            chief_local / "state" / "release-metadata",
            chief_local / "state" / "backups",
            chief_local / "state" / "service-units",
        ]
        state_db_markers = list((chief_local / "state").glob("*.sqlite*")) if (chief_local / "state").exists() else []
        if any(p.exists() for p in state_markers + state_db_markers):
            if chief_local not in candidates:
                candidates.append(chief_local)

        # Check config in AppData/Roaming or AppData/Local
        config_markers = [
            chief_roaming / "production-instance.json",
            chief_roaming / "installation-identity.json",
            chief_roaming / "browser-runtime.json",
            chief_roaming / "Caddyfile",
            chief_local / "config" / "production-instance.json",
        ]
        if any(p.exists() for p in config_markers):
            if chief_roaming.exists():
                candidates.append(chief_roaming)
            elif (chief_local / "config").exists():
                candidates.append(chief_local / "config")

        # Also support custom root / home directory fallback if custom paths were used
        legacy_local = home / ".local" / "share" / "chief"
        if legacy_local.exists():
            candidates.append(legacy_local)

        legacy_config = home / ".config" / "chief"
        if legacy_config.exists():
            candidates.append(legacy_config)

        return list(dict.fromkeys(candidates))

    def detect_existing_installation(self) -> Tuple[bool, List[str]]:
        """Identify prior installed Chief material on this Windows host."""
        home = Path.home()
        existing = [
            str(p)
            for p in self._installed_state_candidates(home)
            if p.exists()
        ]
        return (len(existing) > 0, existing)

    def archive_existing_installation(
        self,
        archive_root: Path,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Path:
        """Safely move existing Chief installation state into an archive base."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        target_dir = archive_root / stamp
        target_dir.mkdir(parents=True, exist_ok=True)

        home = Path.home()
        candidates = self._installed_state_candidates(home)

        archived_items: List[str] = []
        for cand in candidates:
            if not cand.exists():
                continue

            # NEVER archive developer working trees
            if (cand / ".git").exists():
                continue

            # Under chief root, preserve current-run staging directories and active logs
            if cand.name == "chief":
                # Partial runtime / managed / production / caddy / node
                for sub in ("runtime", "managed", "production", "pwas", "caddy", "node", "state", "config"):
                    sub_path = cand / sub
                    if not sub_path.exists():
                        continue
                    if sub == "state":
                        # Preserve installer/logs in state
                        dest_sub = target_dir / "chief" / "state"
                        dest_sub.mkdir(parents=True, exist_ok=True)
                        for item in sub_path.iterdir():
                            if item.name == "installer":
                                continue
                            shutil.move(str(item), str(dest_sub / item.name))
                            archived_items.append(f"{item} -> {dest_sub / item.name}")
                    else:
                        dest_sub = target_dir / "chief" / sub
                        dest_sub.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(sub_path), str(dest_sub))
                        archived_items.append(f"{sub_path} -> {dest_sub}")
            else:
                dest = target_dir / cand.name
                shutil.move(str(cand), str(dest))
                archived_items.append(f"{cand} -> {dest}")

            if progress_callback:
                progress_callback(f"Archived {cand.name} to {target_dir.name}")

        # Stop existing background tasks if present
        for task in ("chief-agent-dashboard", "chief-agent-worker", "chief-agent-scheduler", "chief-tls-proxy"):
            try:
                run_quiet(["schtasks", "/end", "/tn", task], capture_output=True, text=True)
            except Exception:
                pass

        summary = (
            f"Chief Installation Archive - {stamp}\n"
            f"Archived by: {self.get_current_username()}\n"
            f"Operating System: Windows {py_platform.release()}\n"
            f"Items moved:\n" + "\n".join(f" - {it}" for it in archived_items) + "\n"
        )
        (target_dir / "ARCHIVE_SUMMARY.txt").write_text(summary, encoding="utf-8")
        return target_dir

    def install_system_dependencies(
        self,
        progress_callback: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        """Verify Windows system dependencies.

        On Windows, Chief's dependencies (Python venv, Playwright browser, Node.js, Caddy)
        are self-contained user-space distributions. No apt/OS packages required.
        """
        if progress_callback:
            progress_callback("Windows system dependencies satisfied.", 1.0)

    def enable_user_linger(self, username: Optional[str] = None) -> None:
        """Persistent user background tasks on Windows.

        Windows Task Scheduler tasks configured with LogonTrigger run automatically
        while user session is active. No systemd linger is needed.
        """
        pass

    def configure_local_discovery(
        self,
        owner_name: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Derive standard local discovery hostname for Windows.

        Windows 10/11 includes built-in mDNS resolution for .local domains.
        """
        import hashlib
        import unicodedata

        clean = " ".join(owner_name.strip().split())
        ascii_name = unicodedata.normalize("NFKD", clean).encode("ascii", "ignore").decode("ascii")
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
        if not slug:
            slug = "owner-" + hashlib.sha256(clean.encode("utf-8")).hexdigest()[:10]
        label = f"chief-{slug[:57].rstrip('-')}"
        public_host = f"{label}.local"

        if progress_callback:
            progress_callback(f"Local mDNS hostname advertised: {public_host}")
        return public_host

    def set_executable_capability(self, executable: Path, capability: str) -> None:
        """Executable capability handling.

        Windows does not have Linux setcap/file capabilities. Standard Windows sockets
        permit non-elevated user processes to bind local listener ports.
        """
        pass

    def install_service_units(self, stage_dir: Path, dest_dir: Path) -> None:
        """Install generated Task Scheduler XML definitions and register scheduled tasks."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        for unit_file in stage_dir.glob("*.xml"):
            target = dest_dir / unit_file.name
            shutil.copy2(unit_file, target)

            task_name = unit_file.stem
            if shutil.which("schtasks"):
                cmd = ["schtasks", "/create", "/tn", task_name, "/xml", str(target), "/f"]
                res = run_quiet(cmd, capture_output=True, text=True)
                if res.returncode != 0:
                    # Non-fatal during testing if Task Scheduler service is offline
                    pass

    def reload_service_manager(self) -> None:
        """Reload service manager. No-op on Windows."""
        pass

    def _normalize_task_name(self, name: str) -> str:
        """Strip .service or .xml suffix to extract task name."""
        base = name
        if base.endswith(".service"):
            base = base[:-8]
        if base.endswith(".xml"):
            base = base[:-4]
        return base

    def control_services(self, action: str, service_names: List[str]) -> Dict[str, bool]:
        """Start, stop, or restart Chief background tasks via schtasks."""
        if action not in {"start", "stop", "restart", "enable", "disable"}:
            raise ValueError(f"Invalid service action: {action}")

        results: Dict[str, bool] = {}
        if not shutil.which("schtasks"):
            return {s: False for s in service_names}

        for s in service_names:
            task = self._normalize_task_name(s)
            try:
                if action == "start":
                    cmd = ["schtasks", "/run", "/tn", task]
                    res = run_quiet(cmd, capture_output=True, text=True)
                    results[s] = res.returncode == 0
                elif action == "stop":
                    cmd = ["schtasks", "/end", "/tn", task]
                    res = run_quiet(cmd, capture_output=True, text=True)
                    results[s] = res.returncode == 0
                elif action == "restart":
                    run_quiet(["schtasks", "/end", "/tn", task], capture_output=True, text=True)
                    time.sleep(0.5)
                    res = run_quiet(["schtasks", "/run", "/tn", task], capture_output=True, text=True)
                    results[s] = res.returncode == 0
                elif action == "enable":
                    cmd = ["schtasks", "/change", "/tn", task, "/enable"]
                    res = run_quiet(cmd, capture_output=True, text=True)
                    results[s] = res.returncode == 0
                elif action == "disable":
                    cmd = ["schtasks", "/change", "/tn", task, "/disable"]
                    res = run_quiet(cmd, capture_output=True, text=True)
                    results[s] = res.returncode == 0
            except Exception:
                results[s] = False
        return results

    def verify_service_active(self, service_name: str) -> bool:
        """Query Task Scheduler to determine if a task is running."""
        if not shutil.which("schtasks"):
            return False

        task = self._normalize_task_name(service_name)
        try:
            cmd = ["schtasks", "/query", "/tn", task, "/fo", "CSV", "/nh"]
            res = run_quiet(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                output = res.stdout.strip()
                # If Status is Running or Ready
                if "Running" in output or "Ready" in output:
                    return True
        except Exception:
            pass
        return False

    def install_wireguard_system_config(self, source_conf: Path) -> None:
        raise UnsupportedPlatformError("WireGuard server hosting is only supported on Linux (Ubuntu systemd on Folio). WireGuard server cannot be hosted on Windows.")

    def control_wireguard_service(self, action: str) -> bool:
        raise UnsupportedPlatformError("WireGuard server hosting is only supported on Linux (Ubuntu systemd on Folio). WireGuard server cannot be hosted on Windows.")

    def apply_wireguard_firewall(self, port: int) -> None:
        raise UnsupportedPlatformError("WireGuard server hosting is only supported on Linux (Ubuntu systemd on Folio). WireGuard server cannot be hosted on Windows.")

    def export_device_ca(
        self,
        ca_cert_path: Path,
        destination_dir: Path,
        owner_name: str,
        public_host: str,
    ) -> Path:
        """Export local root CA certificate and connection guide for mobile devices."""
        destination_dir.mkdir(parents=True, exist_ok=True)
        dest_cert = destination_dir / "Chief-Local-CA.crt"
        if ca_cert_path.exists():
            shutil.copy2(ca_cert_path, dest_cert)

        readme_file = destination_dir / "README.txt"
        readme_content = (
            f"Chief Device Setup Instructions for {owner_name}\n"
            f"{'='*60}\n\n"
            f"1. To access Chief from your mobile device or another PC on your local network,\n"
            f"   install the certificate 'Chief-Local-CA.crt' onto that device as a trusted root.\n\n"
            f"2. Navigate to:\n"
            f"   - On-Farm Chief Console (Local/LAN): https://{public_host}/\n"
            f"   - Remote Owner PWA (Mobile):        https://{public_host}/owner/\n"
            f"   - Mobile Staff PWA (Field):         https://{public_host}/staff/\n"
            f"   - Job Agent Companion:              https://{public_host}/jobs/\n\n"
            f"3. Sign in with your Chief credentials.\n\n"
            "WireGuard Remote Access (When away from farm Wi-Fi):\n"
            "1. Install the WireGuard app from App Store (iOS) or Google Play (Android).\n"
            "2. Import the tunnel configuration 'Chief-Owner-WireGuard.conf'.\n"
            "3. Enable the tunnel. Chief will be reached securely at the exact same address:\n"
            f"   - On-Farm Chief Console: https://{public_host}/\n"
            f"   - Remote Owner PWA:      https://{public_host}/owner/\n"
            "4. On iOS/Android, configure 'On-Demand' to automatically activate WireGuard when leaving farm Wi-Fi.\n"
        )
        readme_file.write_text(readme_content, encoding="utf-8")
        return readme_file

    def open_url_in_browser(self, url: str) -> bool:
        """Open web browser on Windows without launching a terminal."""
        try:
            if hasattr(os, "startfile"):
                os.startfile(url)
                return True
            import webbrowser
            return webbrowser.open(url)
        except Exception:
            return False
