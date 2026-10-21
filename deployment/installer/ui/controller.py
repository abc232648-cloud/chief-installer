"""Controller for the Chief Unified Installer graphical interface.

Coordinates wizard navigation, validation, and event handling without containing
operating system or deployment business logic. Can be tested headlessly.
"""
from enum import Enum
import re
import threading
from typing import Any, Callable, Dict, List, Optional

from ..errors import InstallerError
from ..events import EventBus, InstallEvent
from ..manifest import CURRENT_RELEASE_MANIFEST, ReleaseManifest
from ..models import InstallOptions, InstallResult, PreflightReport, StepId, StepStatus
from ..orchestrator import InstallerOrchestrator
from ..platforms.base import PlatformAdapter


class ScreenId(str, Enum):
    WELCOME = "WELCOME"
    PREFLIGHT = "PREFLIGHT"
    EXISTING_INSTALL = "EXISTING_INSTALL"
    IDENTITY = "IDENTITY"
    PASSWORD = "PASSWORD"
    FARM_OPERATIONS = "FARM_OPERATIONS"
    CONFIRMATION = "CONFIRMATION"
    PROGRESS = "PROGRESS"
    FAILURE = "FAILURE"
    COMPLETION = "COMPLETION"
    UNINSTALL_OPTIONS = "UNINSTALL_OPTIONS"
    UNINSTALL_PROGRESS = "UNINSTALL_PROGRESS"
    UNINSTALL_RESULT = "UNINSTALL_RESULT"


_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class InstallerUIController:
    """Manages GUI wizard screens, data validation, and orchestrator execution."""

    def __init__(
        self,
        platform_adapter: PlatformAdapter,
        manifest: Optional[ReleaseManifest] = None,
        orchestrator_factory: Optional[Callable[[InstallOptions], InstallerOrchestrator]] = None,
        paths: Optional[Any] = None,
    ):
        self.platform = platform_adapter
        self.manifest = manifest or CURRENT_RELEASE_MANIFEST
        self._orchestrator_factory = orchestrator_factory
        self.paths = paths

        self.current_screen: ScreenId = ScreenId.WELCOME
        self.screen_history: List[ScreenId] = []

        # Form state
        self.dns_mode: str = "quad9"
        self.owner_name: str = ""
        self.owner_username: str = "owner"
        self.owner_password: str = ""
        self.confirm_password: str = ""
        self.farm_operations: str = "DISABLED"
        self.organization_name: str = ""
        self.farm_name: str = ""
        self.acknowledged_archive: bool = False

        # Status & results
        self.preflight_report: Optional[PreflightReport] = None
        self.install_result: Optional[InstallResult] = None
        self.install_events: List[InstallEvent] = []
        self.current_step_display: str = ""
        self.current_step_message: str = ""
        self.current_progress: float = 0.0
        self.failed_reason: str = ""
        self.failed_step: str = ""
        self.diagnostic_report: str = ""

        # Uninstall state
        self.uninstall_mode: str = "standard"
        self.purge_confirmed: bool = False
        self.uninstall_result: Optional[Dict[str, Any]] = None
        self.uninstall_error: str = ""
        self.uninstall_message: str = ""

        # Event bus & listeners
        self.event_bus = EventBus()
        self.event_bus.subscribe(self._handle_orchestrator_event)
        self._screen_listeners: List[Callable[[ScreenId], None]] = []
        self._progress_listeners: List[Callable[[InstallEvent], None]] = []

        self._active_orchestrator: Optional[InstallerOrchestrator] = None

    def subscribe_screen_change(self, listener: Callable[[ScreenId], None]) -> None:
        self._screen_listeners.append(listener)

    def subscribe_progress(self, listener: Callable[[InstallEvent], None]) -> None:
        self._progress_listeners.append(listener)

    def set_screen(self, screen: ScreenId) -> None:
        self.screen_history.append(self.current_screen)
        self.current_screen = screen
        for listener in self._screen_listeners:
            try:
                listener(screen)
            except Exception:
                pass

    def go_back(self) -> None:
        if self.screen_history:
            prev = self.screen_history.pop()
            self.current_screen = prev
            for listener in self._screen_listeners:
                try:
                    listener(prev)
                except Exception:
                    pass

    # Navigation & Validation methods
    def next_from_welcome(self) -> None:
        self.set_screen(ScreenId.PREFLIGHT)
        self.run_preflight_async()

    def run_preflight_async(self, callback: Optional[Callable[[PreflightReport], None]] = None) -> None:
        def worker():
            report = self.platform.run_preflight_checks()
            has_existing, paths = self.platform.detect_existing_installation()
            report.existing_install_detected = has_existing
            report.existing_paths = paths
            self.preflight_report = report
            if callback:
                callback(report)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def next_from_preflight(self) -> None:
        if not self.preflight_report or not self.preflight_report.passed:
            raise ValueError("Preflight checks must pass before continuing.")
        if self.preflight_report.existing_install_detected:
            self.set_screen(ScreenId.EXISTING_INSTALL)
        else:
            self.set_screen(ScreenId.IDENTITY)

    def next_from_existing_install(self, acknowledged: bool) -> None:
        if not acknowledged:
            raise ValueError("You must acknowledge that previous Chief data will be archived before continuing.")
        self.acknowledged_archive = True
        self.set_screen(ScreenId.IDENTITY)

    def validate_identity(self, owner_name: str, username: str) -> None:
        clean_name = owner_name.strip()
        if not clean_name:
            raise ValueError("Please enter who Chief is for (your name or organisation).")
        if len(clean_name) > 100:
            raise ValueError("Name exceeds 100 characters.")

        clean_user = username.strip().lower()
        if not clean_user or not _USERNAME_RE.fullmatch(clean_user):
            raise ValueError("Owner username must be 1-32 lowercase characters (letters, digits, underscore, hyphen).")

        self.owner_name = clean_name
        self.owner_username = clean_user
        self.set_screen(ScreenId.PASSWORD)

    def validate_password(self, p1: str, p2: str) -> None:
        if not p1:
            raise ValueError("Password cannot be empty.")
        if len(p1) < 8:
            raise ValueError("Password must be at least 8 characters.")
        if p1 != p2:
            raise ValueError("Passwords do not match.")

        self.owner_password = p1
        self.confirm_password = p2
        self.set_screen(ScreenId.FARM_OPERATIONS)

    def validate_farm_operations(self, enabled: bool, org_name: str = "", farm_name: str = "") -> None:
        if enabled:
            self.farm_operations = "ENABLED"
            clean_org = org_name.strip() or self.owner_name
            clean_farm = farm_name.strip()
            if not clean_farm:
                raise ValueError("First farm name is required when Farm operations are enabled.")
            self.organization_name = clean_org
            self.farm_name = clean_farm
        else:
            self.farm_operations = "DISABLED"
            self.organization_name = ""
            self.farm_name = ""

        self.set_screen(ScreenId.CONFIRMATION)

    def get_confirmation_summary(self) -> Dict[str, str]:
        return {
            "Release": self.manifest.release_id,
            "Chief DNS": "Quad9 Secure DoH" if self.dns_mode == "quad9" else "System DNS",
            "Chief Owner": self.owner_name,
            "Owner Username": self.owner_username,
            "Farm Operations": "Enabled" if self.farm_operations == "ENABLED" else "Disabled",
            "Initial Farm": f"{self.farm_name} ({self.organization_name})" if self.farm_operations == "ENABLED" else "None",
            "Archive Mode": "Archiving previous installations" if (self.preflight_report and self.preflight_report.existing_install_detected) else "Clean installation",
        }

    def start_installation(self) -> None:
        self.set_screen(ScreenId.PROGRESS)

        options = InstallOptions(
            owner_name=self.owner_name,
            dns_mode=self.dns_mode,
            owner_username=self.owner_username,
            owner_password=self.owner_password,
            farm_operations=self.farm_operations,
            organization_name=self.organization_name if self.farm_operations == "ENABLED" else None,
            farm_name=self.farm_name if self.farm_operations == "ENABLED" else None,
        )

        if self._orchestrator_factory:
            orchestrator = self._orchestrator_factory(options)
        else:
            orchestrator = InstallerOrchestrator(
                platform=self.platform,
                options=options,
                manifest=self.manifest,
                event_bus=self.event_bus,
            )

        self._active_orchestrator = orchestrator

        def worker():
            res = orchestrator.execute_all()
            self.install_result = res
            if res.status == "SUCCESS":
                self.set_screen(ScreenId.COMPLETION)
            elif res.status == "CANCELLED":
                self.failed_reason = "Installation was cancelled."
                self.set_screen(ScreenId.FAILURE)
            else:
                self.failed_reason = res.error or "Unknown installation error."
                self.failed_step = orchestrator.current_step.value
                self.diagnostic_report = orchestrator.logger.generate_diagnostic_report(
                    failed_step=self.failed_step,
                    error=ValueError(self.failed_reason),
                )
                self.set_screen(ScreenId.FAILURE)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def cancel_installation(self) -> None:
        if self._active_orchestrator:
            self._active_orchestrator.cancel()

    def _handle_orchestrator_event(self, event: InstallEvent) -> None:
        self.install_events.append(event)
        self.current_step_display = event.display_name
        self.current_step_message = event.message
        self.current_progress = event.progress

        for listener in self._progress_listeners:
            try:
                listener(event)
            except Exception:
                pass

    def open_chief_console(self) -> bool:
        """Launch the on-farm Chief Console desktop application."""
        try:
            from ..console import launch_console_window, probe_local_console
            from ..orchestrator import InstallerPaths
            paths = InstallerPaths.create()
            diag = probe_local_console(paths)
            if diag.state.value == "HEALTHY":
                if launch_console_window(diag, paths):
                    return True
        except Exception:
            pass

        url = self.install_result.urls.get("chief") if self.install_result and self.install_result.urls else None
        if url:
            return self.platform.open_url_in_browser(url)
        return False

    def open_chief_url(self, app_name: str = "chief") -> bool:
        if app_name == "chief":
            return self.open_chief_console()
        if not self.install_result or not self.install_result.urls:
            return False
        url = self.install_result.urls.get(app_name)
        if not url and app_name == "owner":
            url = self.install_result.urls.get("chief")
        if url:
            return self.platform.open_url_in_browser(url)
        return False

    def start_uninstall(self) -> None:
        """Navigate to uninstallation mode selection screen."""
        self.uninstall_mode = "standard"
        self.purge_confirmed = False
        self.uninstall_result = None
        self.uninstall_error = ""
        self.uninstall_message = ""
        self.set_screen(ScreenId.UNINSTALL_OPTIONS)

    def set_uninstall_mode(self, mode: str) -> None:
        if mode not in ("standard", "purge"):
            raise ValueError(f"Invalid uninstall mode: {mode}")
        self.uninstall_mode = mode

    def set_purge_confirmed(self, confirmed: bool) -> None:
        self.purge_confirmed = bool(confirmed)

    def execute_uninstall(self, callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        """Run uninstallation or purge asynchronously."""
        if self.uninstall_mode == "purge" and not self.purge_confirmed:
            raise ValueError("Confirmation is required before full removal of user data and credentials.")

        self.set_screen(ScreenId.UNINSTALL_PROGRESS)
        self.uninstall_message = "Stopping services and removing components..."

        def worker():
            from ..orchestrator import uninstall_chief, InstallerPaths
            purge = (self.uninstall_mode == "purge")
            try:
                paths = self.paths or InstallerPaths.create()
                report = uninstall_chief(paths=paths, platform=self.platform, purge=purge, confirmed=self.purge_confirmed)
                self.uninstall_result = report
                if not report.get("success", False):
                    failed = report.get("failed_items", [])
                    self.uninstall_error = "; ".join(failed) if failed else "Uninstallation encountered errors."
                self.set_screen(ScreenId.UNINSTALL_RESULT)
            except Exception as exc:
                self.uninstall_error = str(exc)
                self.uninstall_result = {
                    "success": False,
                    "failed_items": [str(exc)],
                    "stopped_services": [],
                    "unregistered_tasks": [],
                    "removed_launchers": [],
                    "removed_paths": [],
                    "purge": purge,
                }
                self.set_screen(ScreenId.UNINSTALL_RESULT)
            if callback and self.uninstall_result:
                callback(self.uninstall_result)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

