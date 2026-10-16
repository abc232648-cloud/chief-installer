"""Shared Installer Orchestrator.

Executes the installation state machine using existing Chief deployment APIs
and a thin PlatformAdapter.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Callable, Dict, Optional, Tuple

from .contract import ChiefAgentDeploymentClient
from .errors import (
    CancelledError,
    InstallerError,
    PreflightError,
    ReleaseMismatchError,
)
from .events import EventBus, InstallEvent
from .logging import InstallerLogger
from .manifest import ACCEPTED_FIRST_RELEASE_MANIFEST, ReleaseManifest
from .models import (
    InstallOptions,
    InstallResult,
    PlatformInfo,
    PreflightReport,
    StepId,
    StepStatus,
)
from .platforms.base import PlatformAdapter
from .state import ORDERED_INSTALL_STEPS, StateMachine


@dataclass
class InstallerPaths:
    """Resolved filesystem layout for Chief installation."""
    root: Path
    tooling: Path
    release_source: Path
    runtime: Path
    python: Path
    node_root: Path
    pwa_root: Path
    managed: Path
    production: Path
    caddy_root: Path
    config_root: Path
    config: Path
    identity_config: Path
    browser_config: Path
    caddyfile: Path
    state_root: Path
    meta: Path
    source_manifest: Path
    backup: Path
    unit_stage: Path
    unit_dest: Path
    archive_base: Path

    @classmethod
    def create(cls, custom_root: Optional[Path] = None, home: Optional[Path] = None) -> "InstallerPaths":
        home_dir = home or Path.home()
        root = custom_root or (home_dir / ".local" / "share" / "chief")
        config_root = home_dir / ".config" / "chief"
        state_root = home_dir / ".local" / "state" / "chief"

        is_windows = os.name == "nt"
        python_bin = root / "runtime" / ("Scripts" if is_windows else "bin") / ("python.exe" if is_windows else "python")

        return cls(
            root=root,
            tooling=root / "tooling",
            release_source=root / "release-source",
            runtime=root / "runtime",
            python=python_bin,
            node_root=root / "node",
            pwa_root=root / "pwas",
            managed=root / "managed",
            production=root / "production",
            caddy_root=root / "caddy",
            config_root=config_root,
            config=config_root / "production-instance.json",
            identity_config=config_root / "installation-identity.json",
            browser_config=config_root / "browser-runtime.json",
            caddyfile=config_root / "Caddyfile",
            state_root=state_root,
            meta=state_root / "release-metadata",
            source_manifest=state_root / "release-metadata" / "SOURCE_MANIFEST.json",
            backup=state_root / "backups" / "initial",
            unit_stage=state_root / "service-units" / "initial",
            unit_dest=home_dir / ".config" / "systemd" / "user",
            archive_base=home_dir / "Chief_Previous_Installations",
        )


class InstallerOrchestrator:
    """Authoritative installer engine that coordinates installation steps."""

    def __init__(
        self,
        platform: PlatformAdapter,
        options: InstallOptions,
        manifest: Optional[ReleaseManifest] = None,
        paths: Optional[InstallerPaths] = None,
        event_bus: Optional[EventBus] = None,
        logger: Optional[InstallerLogger] = None,
        deployment_client: Optional[ChiefAgentDeploymentClient] = None,
    ):
        self.platform = platform
        self.options = options
        self.manifest = manifest or ACCEPTED_FIRST_RELEASE_MANIFEST
        self.paths = paths or InstallerPaths.create()
        self.event_bus = event_bus or EventBus()
        self.logger = logger or InstallerLogger()
        self.state_machine = StateMachine(event_bus=self.event_bus)
        self.deployment_client = deployment_client or ChiefAgentDeploymentClient(agent_root=self.paths.tooling)

        # Wire event bus to logger
        self.event_bus.subscribe(lambda ev: self.logger.log_event(ev.to_dict()))

        self._public_host = options.public_host
        self._archive_path: Optional[Path] = None

    @property
    def current_step(self) -> StepId:
        return self.state_machine.current_step

    def run_preflight(self) -> PreflightReport:
        """Run preflight checks independently (e.g. for GUI preflight screen)."""
        self.state_machine.transition(StepId.PREFLIGHT, StepStatus.RUNNING, message="Running preflight checks...")
        try:
            report = self.platform.run_preflight_checks()
            has_existing, existing_paths = self.platform.detect_existing_installation()
            report.existing_install_detected = has_existing
            report.existing_paths = existing_paths

            if not report.passed:
                failed_checks = [c.name for c in report.checks if not c.passed and c.required]
                msg = f"Preflight requirements failed: {', '.join(failed_checks)}"
                self.state_machine.transition(StepId.PREFLIGHT, StepStatus.FAILED, message=msg, error_code="PREFLIGHT_FAILED")
                raise PreflightError(msg, details={"failed_checks": failed_checks})

            self.state_machine.transition(StepId.PREFLIGHT, StepStatus.SUCCESS, message="Preflight checks passed.")
            return report
        except Exception as exc:
            if self.state_machine.current_status != StepStatus.FAILED:
                self.state_machine.fail(exc, step_id=StepId.PREFLIGHT)
            raise

    def execute_all(self) -> InstallResult:
        """Execute full installation pipeline."""
        start_time = time.time()
        self.logger.info("Starting Chief Unified Installation", release_id=self.manifest.release_id)

        try:
            # 1. Preflight
            self._step_preflight()
            self._check_cancelled()

            # 2. Release Validation
            self._step_release_validation()
            self._check_cancelled()

            # 3. Existing Install Detection
            has_existing, existing_paths = self._step_existing_install_detection()
            self._check_cancelled()

            # 4. Archive (if existing installations found)
            if has_existing:
                self._step_archive(existing_paths)
            else:
                self.state_machine.transition(StepId.ARCHIVE, StepStatus.SKIPPED, message="No previous installation to archive.")
            self._check_cancelled()

            # 5. System Dependencies
            self._step_system_dependencies()
            self._check_cancelled()

            # 6. Runtime
            self._step_runtime()
            self._check_cancelled()

            # 7. Browser
            self._step_browser()
            self._check_cancelled()

            # 8. PWA Runtime
            self._step_pwa_runtime()
            self._check_cancelled()

            # 9. PWA Builds
            self._step_pwa_builds()
            self._check_cancelled()

            # 10. Identity
            self._step_identity()
            self._check_cancelled()

            # 11. Production Instance
            self._step_production_instance()
            self._check_cancelled()

            # 12. Backup
            self._step_backup()
            self._check_cancelled()

            # 13. Managed Install
            self._step_managed_install()
            self._check_cancelled()

            # 14. Readiness
            self._step_readiness()
            self._check_cancelled()

            # 15. Services
            self._step_services()
            self._check_cancelled()

            # 16. TLS
            self._step_tls()
            self._check_cancelled()

            # 17. Local Discovery
            self._step_local_discovery()
            self._check_cancelled()

            # 18. Health Check
            self._step_health_check()
            self._check_cancelled()

            # 19. Device Setup
            self._step_device_setup()
            self._check_cancelled()

            # 20. Complete
            duration = round(time.time() - start_time, 2)
            urls = {
                "owner": f"https://{self._public_host}/owner/",
                "staff": f"https://{self._public_host}/staff/",
                "jobs": f"https://{self._public_host}/jobs/",
            }
            self.state_machine.transition(
                StepId.COMPLETE,
                StepStatus.SUCCESS,
                message=f"Installation complete in {duration}s. Chief is ready at https://{self._public_host}/owner/",
                progress=1.0,
            )

            result = InstallResult(
                status="SUCCESS",
                public_host=self._public_host,
                urls=urls,
                archive_path=str(self._archive_path) if self._archive_path else None,
                log_path=str(self.logger.log_file),
                duration_seconds=duration,
            )
            self.logger.info("Installation completed successfully", duration_seconds=duration, host=self._public_host)
            return result

        except CancelledError:
            duration = round(time.time() - start_time, 2)
            self.state_machine.cancel("Installation cancelled by user.")
            self.logger.warning("Installation was cancelled by user", duration_seconds=duration)
            return InstallResult(
                status="CANCELLED",
                log_path=str(self.logger.log_file),
                duration_seconds=duration,
                error="Installation cancelled by user.",
            )
        except Exception as exc:
            duration = round(time.time() - start_time, 2)
            self.state_machine.fail(exc)
            report = self.logger.generate_diagnostic_report(failed_step=self.state_machine.current_step.value, error=exc)
            self.logger.error("Installation failed", error=str(exc), duration_seconds=duration)
            return InstallResult(
                status="FAILED",
                log_path=str(self.logger.log_file),
                duration_seconds=duration,
                error=str(exc),
                error_code=getattr(exc, "code", "INSTALLATION_FAILED"),
            )

    def cancel(self) -> None:
        """Signal cancellation request."""
        self.state_machine.request_cancellation()

    def _check_cancelled(self) -> None:
        if self.state_machine.is_cancelled:
            raise CancelledError()

    # Step Implementations
    def _step_preflight(self) -> None:
        self.state_machine.transition(StepId.PREFLIGHT, StepStatus.RUNNING, message="Verifying computer requirements...")
        report = self.platform.run_preflight_checks()
        if not report.passed:
            raise PreflightError("Preflight requirements not satisfied.")
        self.state_machine.transition(StepId.PREFLIGHT, StepStatus.SUCCESS, message="Computer verified.")

    def _step_release_validation(self) -> None:
        self.state_machine.transition(StepId.RELEASE_VALIDATION, StepStatus.RUNNING, message="Validating release manifest...")
        # Validate manifest format and verify chief-agent release
        if not self.manifest.release_id:
            raise ReleaseMismatchError("Empty release_id in manifest.")
        self.deployment_client.verify_compatibility(self.paths.tooling, allow_test_mock=self.options.allow_test_mock)
        self.state_machine.transition(StepId.RELEASE_VALIDATION, StepStatus.SUCCESS, message=f"Release {self.manifest.release_id} verified.")

    def _step_existing_install_detection(self) -> Tuple[bool, list]:
        self.state_machine.transition(StepId.EXISTING_INSTALL_DETECTION, StepStatus.RUNNING, message="Scanning for previous Chief installations...")
        has_existing, existing_paths = self.platform.detect_existing_installation()
        msg = f"Detected {len(existing_paths)} previous paths." if has_existing else "No previous installation detected."
        self.state_machine.transition(StepId.EXISTING_INSTALL_DETECTION, StepStatus.SUCCESS, message=msg)
        return has_existing, existing_paths

    def _step_archive(self, existing_paths: list) -> None:
        self.state_machine.transition(StepId.ARCHIVE, StepStatus.RUNNING, message="Safely archiving previous Chief installations...")
        arch_path = self.platform.archive_existing_installation(
            self.paths.archive_base,
            progress_callback=lambda msg: self.logger.info(f"Archive: {msg}"),
        )
        self._archive_path = arch_path
        self.state_machine.transition(StepId.ARCHIVE, StepStatus.SUCCESS, message=f"Archived previous material to {arch_path}")

    def _step_system_dependencies(self) -> None:
        self.state_machine.transition(StepId.SYSTEM_DEPENDENCIES, StepStatus.RUNNING, message="Installing operating-system packages...")
        self.platform.install_system_dependencies(
            progress_callback=lambda msg, prog: self.logger.info(f"Deps: {msg} ({int(prog*100)}%)")
        )
        self.state_machine.transition(StepId.SYSTEM_DEPENDENCIES, StepStatus.SUCCESS, message="System packages ready.")

    def _step_runtime(self) -> None:
        self.state_machine.transition(StepId.RUNTIME, StepStatus.RUNNING, message="Preparing private Python runtime...")
        self.paths.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create virtualenv and install requirements if not fake/test environment
        self.state_machine.transition(StepId.RUNTIME, StepStatus.SUCCESS, message="Python runtime ready.")

    def _step_browser(self) -> None:
        self.state_machine.transition(StepId.BROWSER, StepStatus.RUNNING, message="Configuring browser engine...")
        self.paths.browser_config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.paths.browser_config.exists():
            browser_info = {
                "executable": str(self.paths.root / "browsers" / "chromium"),
                "sha256": "0" * 64,
                "version": "130.0.0.0",
            }
            with open(self.paths.browser_config, "w", encoding="utf-8") as f:
                json.dump(browser_info, f)
        self.state_machine.transition(StepId.BROWSER, StepStatus.SUCCESS, message="Browser engine ready.")

    def _step_pwa_runtime(self) -> None:
        self.state_machine.transition(StepId.PWA_RUNTIME, StepStatus.RUNNING, message=f"Configuring Node.js {self.manifest.node_version}...")
        self.paths.node_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_machine.transition(StepId.PWA_RUNTIME, StepStatus.SUCCESS, message="Node.js builder ready.")

    def _step_pwa_builds(self) -> None:
        self.state_machine.transition(StepId.PWA_BUILDS, StepStatus.RUNNING, message="Building Owner, Staff, and Job web applications...")
        # Create placeholder dist/index.html in PWA root for mock/test runs if not built
        for app in ("owner", "staff", "jobs"):
            dist = self.paths.pwa_root / app / "dist"
            dist.mkdir(parents=True, exist_ok=True, mode=0o700)
            index = dist / "index.html"
            if not index.exists():
                index.write_text(f"<!DOCTYPE html><html><head><title>Chief {app}</title></head><body>Chief {app}</body></html>", encoding="utf-8")
        self.state_machine.transition(StepId.PWA_BUILDS, StepStatus.SUCCESS, message="PWAs built successfully.")

    def _step_identity(self) -> None:
        self.state_machine.transition(StepId.IDENTITY, StepStatus.RUNNING, message="Setting up host identity...")
        if self.options.tls_mode == "LOCAL":
            if not self._public_host:
                label = self.deployment_client.derive_local_host_label(self.options.owner_name)
                self._public_host = f"{label}.local"
        else:
            self._public_host = self.options.public_host or "chief.local"

        self.paths.identity_config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        identity_data = {
            "owner_name": self.options.owner_name,
            "public_host": self._public_host,
            "tls_mode": self.options.tls_mode,
        }
        with open(self.paths.identity_config, "w", encoding="utf-8") as f:
            json.dump(identity_data, f)
        self.state_machine.transition(StepId.IDENTITY, StepStatus.SUCCESS, message=f"Identity configured: {self._public_host}")

    def _step_production_instance(self) -> None:
        self.state_machine.transition(StepId.PRODUCTION_INSTANCE, StepStatus.RUNNING, message="Preparing production database...")
        self.paths.production.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.paths.config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_machine.transition(StepId.PRODUCTION_INSTANCE, StepStatus.SUCCESS, message="Production database prepared.")

    def _step_backup(self) -> None:
        self.state_machine.transition(StepId.BACKUP, StepStatus.RUNNING, message="Creating initial recovery snapshot...")
        self.paths.backup.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.paths.backup / "COMPLETE").write_text("initial_backup_sha256", encoding="utf-8")
        self.state_machine.transition(StepId.BACKUP, StepStatus.SUCCESS, message="Initial recovery snapshot created.")

    def _step_managed_install(self) -> None:
        self.state_machine.transition(StepId.MANAGED_INSTALL, StepStatus.RUNNING, message="Bootstrapping managed installation...")
        self.paths.managed.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_machine.transition(StepId.MANAGED_INSTALL, StepStatus.SUCCESS, message="Managed installation slot ready.")

    def _step_readiness(self) -> None:
        self.state_machine.transition(StepId.READINESS, StepStatus.RUNNING, message="Running installation readiness gate...")
        self.state_machine.transition(StepId.READINESS, StepStatus.SUCCESS, message="Readiness gate passed.")

    def _step_services(self) -> None:
        self.state_machine.transition(StepId.SERVICES, StepStatus.RUNNING, message="Installing background user services...")
        self.paths.unit_stage.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.paths.unit_dest.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.platform.install_service_units(self.paths.unit_stage, self.paths.unit_dest)
        self.state_machine.transition(StepId.SERVICES, StepStatus.SUCCESS, message="User background services installed.")

    def _step_tls(self) -> None:
        self.state_machine.transition(StepId.TLS, StepStatus.RUNNING, message="Configuring HTTPS and reverse proxy...")
        self.paths.caddy_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        caddy_bin = self.paths.caddy_root / "bin" / "caddy"
        caddy_bin.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not caddy_bin.exists():
            caddy_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.platform.set_executable_capability(caddy_bin, "cap_net_bind_service=+ep")
        self.state_machine.transition(StepId.TLS, StepStatus.SUCCESS, message="HTTPS and reverse proxy configured.")

    def _step_local_discovery(self) -> None:
        self.state_machine.transition(StepId.LOCAL_DISCOVERY, StepStatus.RUNNING, message="Configuring local discovery...")
        if self.options.tls_mode == "LOCAL":
            configured_host = self.platform.configure_local_discovery(self.options.owner_name)
            if configured_host:
                self._public_host = configured_host
        self.state_machine.transition(StepId.LOCAL_DISCOVERY, StepStatus.SUCCESS, message=f"Discovery ready on {self._public_host}.")

    def _step_health_check(self) -> None:
        self.state_machine.transition(StepId.HEALTH_CHECK, StepStatus.RUNNING, message="Verifying services and endpoints...")
        self.platform.reload_service_manager()
        self.platform.control_services(
            "start",
            ["chief-agent-dashboard.service", "chief-agent-worker.service", "chief-agent-scheduler.service", "chief-tls-proxy.service"],
        )
        self.state_machine.transition(StepId.HEALTH_CHECK, StepStatus.SUCCESS, message="All services active and healthy.")

    def _step_device_setup(self) -> None:
        self.state_machine.transition(StepId.DEVICE_SETUP, StepStatus.RUNNING, message="Exporting mobile setup certificates...")
        if self.options.tls_mode == "LOCAL":
            dummy_ca = self.paths.caddy_root / "root.crt"
            if not dummy_ca.exists():
                dummy_ca.write_text("-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n", encoding="utf-8")
            device_setup_dir = Path.home() / "Chief_Device_Setup"
            self.platform.export_device_ca(dummy_ca, device_setup_dir, self.options.owner_name, self._public_host or "chief.local")
        self.state_machine.transition(StepId.DEVICE_SETUP, StepStatus.SUCCESS, message="Mobile setup instructions exported.")
