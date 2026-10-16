"""Shared Installer Orchestrator.

Executes the installation state machine using existing Chief deployment APIs
and a thin PlatformAdapter.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
from typing import Callable, Dict, List, Optional, Tuple
import urllib.request

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
        self.deployment_client = deployment_client or ChiefAgentDeploymentClient(
            agent_root=self.paths.tooling,
            python_bin=self.paths.python,
        )

        # Wire event bus to logger
        self.event_bus.subscribe(lambda ev: self.logger.log_event(ev.to_dict()))

        self._public_host = options.public_host
        self._archive_path: Optional[Path] = None
        self._expected_source_sha256: Optional[str] = None
        self._expected_schema_sha256: Optional[str] = None
        self._backup_manifest_sha: Optional[str] = None

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
        self.logger.info(
            "Starting Chief Unified Installation",
            release_id=self.manifest.release_id,
            owner_name=self.options.owner_name,
            owner_username=self.options.owner_username,
            owner_password="[REDACTED]",
        )

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

    def _is_test_fixture(self) -> bool:
        """Check if running under an explicit test fixture."""
        return (
            bool(self.options.allow_test_mock)
            or self.platform.__class__.__name__ == "FakePlatformAdapter"
            or getattr(self.platform, "is_mock", False)
        )

    # Repository & Runtime Helpers
    def _acquire_repository(
        self,
        target_dir: Path,
        repo_url: str,
        commit: str,
        component: str,
    ) -> None:
        """Acquire a component repository at an exact verified commit SHA."""
        target_dir = Path(target_dir)
        git_dir = target_dir / ".git"

        local_override = None
        if component == "chief_agent":
            env_override = os.environ.get("CHIEF_AGENT_SOURCE")
            if env_override and Path(env_override).is_dir():
                local_override = Path(env_override)
            else:
                cand = Path(__file__).resolve().parents[3] / "chief-agent"
                if cand.is_dir() and (cand / ".git").is_dir():
                    local_override = cand

        clone_source = str(local_override) if local_override else repo_url

        if not target_dir.exists() or not any(target_dir.iterdir()):
            target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.logger.info(f"Cloning {component} from {clone_source} at commit {commit}")
            res = subprocess.run(
                ["git", "clone", clone_source, str(target_dir)],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                raise InstallerError(f"Failed to clone {component} from {clone_source}: {res.stderr.strip()}")
            res = subprocess.run(
                ["git", "-C", str(target_dir), "checkout", commit],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                raise InstallerError(f"Failed to checkout {component} commit {commit}: {res.stderr.strip()}")
        else:
            if git_dir.exists():
                res = subprocess.run(
                    ["git", "-C", str(target_dir), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                )
                actual_head = res.stdout.strip().lower()
                if actual_head != commit.lower():
                    checkout_res = subprocess.run(
                        ["git", "-C", str(target_dir), "checkout", commit],
                        capture_output=True,
                        text=True,
                    )
                    if checkout_res.returncode != 0:
                        fetch_src = str(local_override) if local_override else "origin"
                        subprocess.run(
                            ["git", "-C", str(target_dir), "fetch", fetch_src],
                            capture_output=True,
                            text=True,
                        )
                        res2 = subprocess.run(
                            ["git", "-C", str(target_dir), "checkout", commit],
                            capture_output=True,
                            text=True,
                        )
                        if res2.returncode != 0:
                            raise InstallerError(f"Failed to checkout {component} commit {commit}: {res2.stderr.strip()}")

        if git_dir.exists():
            res = subprocess.run(
                ["git", "-C", str(target_dir), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0:
                self.manifest.verify_component_commit(component, res.stdout.strip())

    def _acquire_node_runtime(self) -> Tuple[Path, Path]:
        """Verify or acquire pinned Node.js runtime and npm binary."""
        node_bin = self.paths.node_root / "bin" / "node"
        npm_bin = self.paths.node_root / "bin" / "npm"
        if node_bin.exists() and npm_bin.exists():
            return node_bin, npm_bin

        sys_node = shutil.which("node")
        sys_npm = shutil.which("npm")
        if sys_node and sys_npm:
            res = subprocess.run([sys_node, "--version"], capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip() == f"v{self.manifest.node_version}":
                return Path(sys_node), Path(sys_npm)

        node_ver = self.manifest.node_version
        machine = os.uname().machine.lower() if hasattr(os, "uname") else "x86_64"
        node_arch = "x64" if machine in ("x86_64", "amd64") else ("arm64" if machine in ("aarch64", "arm64") else "x64")
        node_file = f"node-v{node_ver}-linux-{node_arch}.tar.xz"
        node_tmp = self.paths.state_root / "node-download"
        node_tmp.mkdir(parents=True, exist_ok=True, mode=0o700)

        shasums_url = f"https://nodejs.org/dist/v{node_ver}/SHASUMS256.txt"
        shasums_path = node_tmp / "SHASUMS256.txt"
        self.logger.info(f"Downloading Node.js SHASUMS256.txt from {shasums_url}")
        urllib.request.urlretrieve(shasums_url, str(shasums_path))

        expected_sha = None
        for line in shasums_path.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1].endswith(node_file):
                expected_sha = parts[0]
                break
        if not expected_sha:
            raise InstallerError(f"Could not find checksum for {node_file} in SHASUMS256.txt")

        tar_url = f"https://nodejs.org/dist/v{node_ver}/{node_file}"
        tar_path = node_tmp / node_file
        self.logger.info(f"Downloading Node.js archive from {tar_url}")
        urllib.request.urlretrieve(tar_url, str(tar_path))

        actual_sha = hashlib.sha256(tar_path.read_bytes()).hexdigest()
        if actual_sha.lower() != expected_sha.lower():
            raise ReleaseMismatchError(f"Node.js archive checksum mismatch: expected {expected_sha}, got {actual_sha}")

        self.paths.node_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tarfile.open(tar_path, "r:xz") as tar:
            for member in tar.getmembers():
                parts = Path(member.name).parts
                if len(parts) > 1:
                    if hasattr(tarfile, "data_filter"):
                        tar.extract(member, path=str(self.paths.node_root), filter="data")
                    else:
                        tar.extract(member, path=str(self.paths.node_root))

        return node_bin, npm_bin

    def _download_and_verify_caddy(self) -> Path:
        """Download Caddy binary, verify checksums, and extract to caddy root."""
        caddy_bin = self.paths.caddy_root / "bin" / "caddy"
        if caddy_bin.exists():
            return caddy_bin

        machine = os.uname().machine.lower() if hasattr(os, "uname") else "x86_64"
        caddy_arch = "amd64" if machine in ("x86_64", "amd64") else ("arm64" if machine in ("aarch64", "arm64") else "amd64")
        caddy_ver = self.manifest.caddy_version
        caddy_file = f"caddy_{caddy_ver}_linux_{caddy_arch}.tar.gz"
        download_tmp = self.paths.state_root / "caddy-download"
        download_tmp.mkdir(parents=True, exist_ok=True, mode=0o700)

        checksums_url = f"https://github.com/caddyserver/caddy/releases/download/v{caddy_ver}/caddy_{caddy_ver}_checksums.txt"
        checksums_path = download_tmp / "checksums.txt"
        self.logger.info(f"Downloading Caddy checksums from {checksums_url}")
        urllib.request.urlretrieve(checksums_url, str(checksums_path))

        actual_checksums_sha256 = hashlib.sha256(checksums_path.read_bytes()).hexdigest()
        if actual_checksums_sha256.lower() != self.manifest.caddy_checksum.lower():
            raise ReleaseMismatchError(
                f"Caddy checksums file verification failed. Expected {self.manifest.caddy_checksum}, got {actual_checksums_sha256}"
            )

        expected_file_sha256 = None
        for line in checksums_path.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1].endswith(caddy_file):
                expected_file_sha256 = parts[0]
                break
        if not expected_file_sha256:
            raise InstallerError(f"Could not find checksum for {caddy_file} in checksums.txt")

        tar_url = f"https://github.com/caddyserver/caddy/releases/download/v{caddy_ver}/{caddy_file}"
        tar_path = download_tmp / caddy_file
        self.logger.info(f"Downloading Caddy binary archive from {tar_url}")
        urllib.request.urlretrieve(tar_url, str(tar_path))

        if len(expected_file_sha256) == 128:
            actual_tar_hash = hashlib.sha512(tar_path.read_bytes()).hexdigest()
        else:
            actual_tar_hash = hashlib.sha256(tar_path.read_bytes()).hexdigest()

        if actual_tar_hash.lower() != expected_file_sha256.lower():
            raise ReleaseMismatchError(
                f"Caddy archive verification failed. Expected {expected_file_sha256}, got {actual_tar_hash}"
            )

        (self.paths.caddy_root / "bin").mkdir(parents=True, exist_ok=True, mode=0o700)
        with tarfile.open(tar_path, "r:gz") as tar:
            member = tar.getmember("caddy")
            tar.extract(member, path=str(self.paths.caddy_root / "bin"))

        caddy_bin.chmod(0o700)
        return caddy_bin

    def generate_caddyfile(self, public_host: str, tls_mode: str) -> str:
        """Generate authoritative Caddy reverse proxy and PWA configuration."""
        tls_line = "    tls internal\n" if tls_mode == "LOCAL" else ""
        return (
            f"{public_host} {{\n"
            f"{tls_line}"
            f"    encode zstd gzip\n"
            f"    header {{\n"
            f'        Strict-Transport-Security "max-age=31536000"\n'
            f'        X-Content-Type-Options "nosniff"\n'
            f'        X-Frame-Options "DENY"\n'
            f'        Referrer-Policy "no-referrer"\n'
            f'        Permissions-Policy "camera=(self), microphone=(), geolocation=()"\n'
            f"    }}\n"
            f"    handle /api/* {{\n"
            f"        reverse_proxy 127.0.0.1:8765 {{\n"
            f"            header_up Host {{host}}\n"
            f"            header_up X-Forwarded-Proto https\n"
            f"        }}\n"
            f"    }}\n"
            f"    handle_path /owner/* {{\n"
            f"        root * {self.paths.pwa_root}/owner/dist\n"
            f"        try_files {{path}} /index.html\n"
            f"        file_server\n"
            f"    }}\n"
            f"    handle_path /staff/* {{\n"
            f"        root * {self.paths.pwa_root}/staff/dist\n"
            f"        try_files {{path}} /index.html\n"
            f"        file_server\n"
            f"    }}\n"
            f"    handle_path /jobs/* {{\n"
            f"        root * {self.paths.pwa_root}/jobs/dist\n"
            f"        try_files {{path}} /index.html\n"
            f"        file_server\n"
            f"    }}\n"
            f"    respond 404\n"
            f"}}\n"
        )

    def generate_caddy_service_unit(self) -> str:
        """Generate systemd user service unit definition for Caddy TLS proxy."""
        return (
            "[Unit]\n"
            "Description=Chief HTTPS and PWA proxy\n"
            "After=network-online.target chief-agent-dashboard.service\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={self.paths.caddy_root}/bin/caddy run --config {self.paths.caddyfile} --adapter caddyfile\n"
            f"ExecReload={self.paths.caddy_root}/bin/caddy reload --config {self.paths.caddyfile} --adapter caddyfile\n"
            "Restart=on-failure\n"
            "RestartSec=10\n"
            "NoNewPrivileges=yes\n"
            "UMask=0077\n"
            f"Environment=XDG_DATA_HOME={self.paths.caddy_root}/data\n"
            f"Environment=XDG_CONFIG_HOME={self.paths.caddy_root}/config\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    # Step Implementations
    def _step_preflight(self) -> None:
        self.state_machine.transition(StepId.PREFLIGHT, StepStatus.RUNNING, message="Verifying computer requirements...")
        report = self.platform.run_preflight_checks()
        if not report.passed:
            raise PreflightError("Preflight requirements not satisfied.")
        self.state_machine.transition(StepId.PREFLIGHT, StepStatus.SUCCESS, message="Computer verified.")

    def _step_release_validation(self) -> None:
        self.state_machine.transition(StepId.RELEASE_VALIDATION, StepStatus.RUNNING, message="Validating release manifest...")
        if not self.manifest.release_id:
            raise ReleaseMismatchError("Empty release_id in manifest.")

        if not self._is_test_fixture():
            repo_url = getattr(self.manifest, "chief_agent_repo", "https://github.com/abc232648-cloud/chief-agent.git")
            if not self.paths.tooling.exists() or not (self.paths.tooling / "deployment" / "contract.py").exists():
                self._acquire_repository(
                    target_dir=self.paths.tooling,
                    repo_url=repo_url,
                    commit=self.manifest.chief_agent_commit,
                    component="chief_agent",
                )
            if not self.paths.release_source.exists():
                self._acquire_repository(
                    target_dir=self.paths.release_source,
                    repo_url=repo_url,
                    commit=self.manifest.chief_agent_commit,
                    component="chief_agent",
                )

        handshake = self.deployment_client.verify_compatibility(
            self.paths.tooling,
            expected_commit=self.manifest.chief_agent_commit,
            allow_test_mock=self.options.allow_test_mock,
        )
        if not self.options.allow_test_mock and handshake.chief_agent_commit.lower() != self.manifest.chief_agent_commit.lower():
            raise ReleaseMismatchError(
                f"Release verification failure: chief_agent commit mismatch. "
                f"Expected {self.manifest.chief_agent_commit}, but contract reported {handshake.chief_agent_commit}."
            )
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
        if self._is_test_fixture():
            self.state_machine.transition(StepId.RUNTIME, StepStatus.SUCCESS, message="Python runtime ready (mock).")
            return

        if not self.paths.python.exists():
            venv_cmd = [sys.executable, "-m", "venv", str(self.paths.runtime)]
            self.logger.info("Creating isolated Python virtual environment", venv_cmd=venv_cmd)
            res = subprocess.run(venv_cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise InstallerError(f"Failed to create Python virtual environment: {res.stderr.strip()}")

            pip_cmd = [str(self.paths.python), "-m", "pip", "install", "--upgrade", "pip"]
            subprocess.run(pip_cmd, capture_output=True, text=True)

            reqs_file = self.paths.tooling / "requirements.txt"
            if reqs_file.exists():
                self.logger.info(f"Installing Chief requirements from {reqs_file}")
                install_cmd = [str(self.paths.python), "-m", "pip", "install", "-r", str(reqs_file)]
                res = subprocess.run(install_cmd, capture_output=True, text=True)
                if res.returncode != 0:
                    raise InstallerError(f"Failed to install requirements: {res.stderr.strip()}")

        self.deployment_client.python_bin = self.paths.python
        self.state_machine.transition(StepId.RUNTIME, StepStatus.SUCCESS, message="Python runtime ready.")

    def _step_browser(self) -> None:
        self.state_machine.transition(StepId.BROWSER, StepStatus.RUNNING, message="Configuring browser engine...")
        self.paths.browser_config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        if self._is_test_fixture():
            if not self.paths.browser_config.exists():
                browser_info = {
                    "executable": str(self.paths.root / "browsers" / "chromium"),
                    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "version": "130.0.0.0",
                }
                with open(self.paths.browser_config, "w", encoding="utf-8") as f:
                    json.dump(browser_info, f, indent=2)
            self.state_machine.transition(StepId.BROWSER, StepStatus.SUCCESS, message="Browser engine ready (mock).")
            return

        if not self.paths.browser_config.exists():
            self.logger.info("Installing Playwright Chromium browser...")
            install_res = subprocess.run(
                [str(self.paths.python), "-m", "playwright", "install", "--with-deps", "chromium"],
                capture_output=True,
                text=True,
            )
            if install_res.returncode != 0:
                raise InstallerError(f"Playwright chromium installation failed: {install_res.stderr.strip()}")

            find_script = (
                "from playwright.sync_api import sync_playwright\n"
                "p = sync_playwright().start()\n"
                "print(p.chromium.executable_path)\n"
                "p.stop()\n"
            )
            path_res = subprocess.run(
                [str(self.paths.python), "-c", find_script],
                capture_output=True,
                text=True,
            )
            if path_res.returncode != 0 or not path_res.stdout.strip():
                raise InstallerError(f"Failed to locate Chromium executable path: {path_res.stderr.strip()}")

            chromium_exec = Path(path_res.stdout.strip())
            if not chromium_exec.exists():
                raise InstallerError(f"Chromium executable does not exist at {chromium_exec}")

            h = hashlib.sha256()
            with open(chromium_exec, "rb") as bf:
                while chunk := bf.read(65536):
                    h.update(chunk)
            chromium_sha256 = h.hexdigest()

            ver_res = subprocess.run([str(chromium_exec), "--version"], capture_output=True, text=True)
            chromium_version = ver_res.stdout.strip() if ver_res.returncode == 0 else "chromium-playwright"

            browser_info = {
                "executable": str(chromium_exec),
                "sha256": chromium_sha256,
                "version": chromium_version,
            }
            with open(self.paths.browser_config, "w", encoding="utf-8") as f:
                json.dump(browser_info, f, indent=2)

        self.state_machine.transition(StepId.BROWSER, StepStatus.SUCCESS, message="Browser engine ready.")

    def _step_pwa_runtime(self) -> None:
        self.state_machine.transition(StepId.PWA_RUNTIME, StepStatus.RUNNING, message=f"Configuring Node.js {self.manifest.node_version}...")
        self.paths.node_root.mkdir(parents=True, exist_ok=True, mode=0o700)

        if self._is_test_fixture():
            self.state_machine.transition(StepId.PWA_RUNTIME, StepStatus.SUCCESS, message="Node.js builder ready (mock).")
            return

        node_bin = self.paths.node_root / "bin" / "node"
        npm_bin = self.paths.node_root / "bin" / "npm"
        if not (node_bin.exists() and npm_bin.exists()):
            self._acquire_node_runtime()
        self.state_machine.transition(StepId.PWA_RUNTIME, StepStatus.SUCCESS, message=f"Node.js {self.manifest.node_version} builder ready.")

    def _step_pwa_builds(self) -> None:
        self.state_machine.transition(StepId.PWA_BUILDS, StepStatus.RUNNING, message="Building Owner, Staff, and Job web applications...")
        if self._is_test_fixture():
            for app in ("owner", "staff", "jobs"):
                dist = self.paths.pwa_root / app / "dist"
                dist.mkdir(parents=True, exist_ok=True, mode=0o700)
                index = dist / "index.html"
                if not index.exists():
                    index.write_text(f"<!DOCTYPE html><html><head><title>Chief {app}</title></head><body>Chief {app}</body></html>", encoding="utf-8")
            self.state_machine.transition(StepId.PWA_BUILDS, StepStatus.SUCCESS, message="PWAs built successfully (mock).")
            return

        all_built = all((self.paths.pwa_root / app / "dist" / "index.html").exists() for app in ("owner", "staff", "jobs"))
        if not all_built:
            pwa_mapping = [
                ("owner", "owner_pwa", "https://github.com/abc232648-cloud/chief-owner-pwa.git"),
                ("staff", "staff_pwa", "https://github.com/abc232648-cloud/chief-staff-pwa.git"),
                ("jobs", "job_pwa", "https://github.com/abc232648-cloud/Job-agent-pwa.git"),
            ]
            node_bin, npm_bin = self._acquire_node_runtime()

            for name, comp, default_repo in pwa_mapping:
                app_dir = self.paths.pwa_root / name
                repo_url = getattr(self.manifest, f"{comp}_repo", default_repo)
                commit = getattr(self.manifest, f"{comp}_commit")
                self._acquire_repository(target_dir=app_dir, repo_url=repo_url, commit=commit, component=comp)

                self.logger.info(f"Building {name} PWA with npm...")
                ci_res = subprocess.run([str(npm_bin), "ci"], cwd=str(app_dir), capture_output=True, text=True)
                if ci_res.returncode != 0:
                    raise InstallerError(f"npm ci failed for {name} PWA: {ci_res.stderr.strip()}")

                build_res = subprocess.run([str(npm_bin), "run", "build"], cwd=str(app_dir), capture_output=True, text=True)
                if build_res.returncode != 0:
                    raise InstallerError(f"npm run build failed for {name} PWA: {build_res.stderr.strip()}")

                dist_index = app_dir / "dist" / "index.html"
                if not dist_index.exists():
                    raise InstallerError(f"{name} PWA build did not produce dist/index.html")

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
            "owner_username": self.options.owner_username,
            "public_host": self._public_host,
            "tls_mode": self.options.tls_mode,
        }
        with open(self.paths.identity_config, "w", encoding="utf-8") as f:
            json.dump(identity_data, f, indent=2)
        self.state_machine.transition(StepId.IDENTITY, StepStatus.SUCCESS, message=f"Identity configured: {self._public_host}")

    def _step_production_instance(self) -> None:
        self.state_machine.transition(StepId.PRODUCTION_INSTANCE, StepStatus.RUNNING, message="Preparing production database...")
        self.paths.production.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.paths.config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.paths.source_manifest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        if self._is_test_fixture():
            if not self.paths.source_manifest.exists():
                with open(self.paths.source_manifest, "w", encoding="utf-8") as f:
                    json.dump({"source_tree_sha256": "mock_source_sha256"}, f)
            self._expected_source_sha256 = "mock_source_sha256"

            if not self.paths.config.exists():
                with open(self.paths.config, "w", encoding="utf-8") as f:
                    json.dump({"schema_sha256": "mock_schema_sha256", "dashboard_port": 8765, "dns_mode": self.options.dns_mode}, f)
            self._expected_schema_sha256 = "mock_schema_sha256"
            self.state_machine.transition(StepId.PRODUCTION_INSTANCE, StepStatus.SUCCESS, message="Production database prepared (mock).")
            return

        self.logger.info("Building source manifest via Chief deployment contract...")
        manifest_res = self.deployment_client.build_source_manifest(
            source_root=self.paths.release_source,
            output=self.paths.source_manifest,
            expected_git_commit=self.manifest.chief_agent_commit,
        )
        self._expected_source_sha256 = manifest_res.get("source_tree_sha256")
        if not self._expected_source_sha256:
            raise InstallerError("Chief build_source_manifest did not return source_tree_sha256")

        self.logger.info("Executing prepare_production via Chief deployment contract...")
        prep_res = self.deployment_client.prepare_production(
            root=self.paths.production,
            config=self.paths.config,
            source_root=self.paths.release_source,
            source_manifest=self.paths.source_manifest,
            username=self.options.owner_username,
            password=self.options.owner_password,
            public_host=self._public_host,
            browser_runtime=self.paths.browser_config,
            dns_mode=self.options.dns_mode,
            dashboard_port=8765,
            trusted_tls_proxy="127.0.0.1",
            farm_operations=self.options.farm_operations,
            organization_name=self.options.organization_name,
            farm_name=self.options.farm_name,
        )
        self._expected_schema_sha256 = prep_res.get("schema_sha256")
        if not self._expected_schema_sha256:
            raise InstallerError("Chief prepare_production did not return schema_sha256")

        self.state_machine.transition(StepId.PRODUCTION_INSTANCE, StepStatus.SUCCESS, message="Production database prepared.")

    def _step_backup(self) -> None:
        self.state_machine.transition(StepId.BACKUP, StepStatus.RUNNING, message="Creating initial recovery snapshot...")
        self.paths.backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        if self._is_test_fixture():
            self.paths.backup.mkdir(parents=True, exist_ok=True, mode=0o700)
            complete_file = self.paths.backup / "COMPLETE"
            if not complete_file.exists():
                complete_file.write_text("initial_backup_sha256", encoding="utf-8")
            self._backup_manifest_sha = "initial_backup_sha256"
            self.state_machine.transition(StepId.BACKUP, StepStatus.SUCCESS, message="Initial recovery snapshot created (mock).")
            return

        if self.paths.backup.exists():
            if self.paths.backup.is_dir():
                shutil.rmtree(self.paths.backup)
            else:
                self.paths.backup.unlink()

        self.logger.info("Creating install backup via Chief deployment contract...")
        self.deployment_client.create_install_backup(
            config=self.paths.config,
            destination=self.paths.backup,
            source_root=self.paths.release_source,
            source_manifest=self.paths.source_manifest,
            private_storage_confirmed=True,
        )
        complete_file = self.paths.backup / "COMPLETE"
        if not complete_file.exists():
            raise InstallerError(f"Backup failed to write COMPLETE file at {complete_file}")
        self._backup_manifest_sha = complete_file.read_text(encoding="utf-8").strip()

        self.state_machine.transition(StepId.BACKUP, StepStatus.SUCCESS, message="Initial recovery snapshot created.")

    def _step_managed_install(self) -> None:
        self.state_machine.transition(StepId.MANAGED_INSTALL, StepStatus.RUNNING, message="Bootstrapping managed installation...")
        self.paths.managed.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        if self._is_test_fixture():
            self.paths.managed.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.state_machine.transition(StepId.MANAGED_INSTALL, StepStatus.SUCCESS, message="Managed installation slot ready (mock).")
            return

        if self.paths.managed.exists():
            if self.paths.managed.is_dir():
                shutil.rmtree(self.paths.managed)
            else:
                self.paths.managed.unlink()

        self.logger.info("Bootstrapping managed installation via Chief deployment contract...")
        self.deployment_client.bootstrap_installation(
            directory=self.paths.managed,
            source_root=self.paths.release_source,
            source_manifest=self.paths.source_manifest,
            expected_source=self._expected_source_sha256,
            python=self.paths.python,
            instance_config=self.paths.config,
            schema_sha256=self._expected_schema_sha256,
            private_storage_confirmed=True,
        )
        self.state_machine.transition(StepId.MANAGED_INSTALL, StepStatus.SUCCESS, message="Managed installation slot ready.")

    def _step_readiness(self) -> None:
        self.state_machine.transition(StepId.READINESS, StepStatus.RUNNING, message="Running installation readiness gate...")
        if self._is_test_fixture():
            self.state_machine.transition(StepId.READINESS, StepStatus.SUCCESS, message="Readiness gate passed (mock).")
            return

        self.logger.info("Executing installation readiness validation via Chief deployment contract...")
        user = self.platform.get_current_username()
        readiness_res = self.deployment_client.installation_readiness(
            config=self.paths.config,
            source_root=self.paths.release_source,
            source_manifest=self.paths.source_manifest,
            backup_directory=self.paths.backup,
            manifest_sha256=self._backup_manifest_sha,
            expected_source=self._expected_source_sha256,
            expected_schema=self._expected_schema_sha256,
            platform="linux",
            project=str(self.paths.tooling),
            user=user,
            python=self.paths.python,
            managed_installation=self.paths.managed,
        )
        if readiness_res.get("status") != "SOURCE_READY_FOR_INSTALL_REVIEW" and not readiness_res.get("ready"):
            raise InstallerError(f"Installation readiness gate failed: {readiness_res}")

        self.state_machine.transition(StepId.READINESS, StepStatus.SUCCESS, message="Readiness gate passed.")

    def _step_services(self) -> None:
        self.state_machine.transition(StepId.SERVICES, StepStatus.RUNNING, message="Installing background user services...")
        self.paths.unit_stage.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.paths.unit_dest.mkdir(parents=True, exist_ok=True, mode=0o700)

        if self._is_test_fixture():
            self.platform.install_service_units(self.paths.unit_stage, self.paths.unit_dest)
            self.state_machine.transition(StepId.SERVICES, StepStatus.SUCCESS, message="User background services installed (mock).")
            return

        self.logger.info("Generating service definitions via Chief deployment contract...")
        user = self.platform.get_current_username()
        self.deployment_client.write_service_definitions(
            target_dir=self.paths.unit_stage,
            platform="linux",
            project=str(self.paths.tooling),
            user=user,
            instance_config=str(self.paths.config),
            python=str(self.paths.python),
            managed_installation=str(self.paths.managed),
        )
        self.platform.install_service_units(self.paths.unit_stage, self.paths.unit_dest)
        self.state_machine.transition(StepId.SERVICES, StepStatus.SUCCESS, message="User background services installed.")

    def _step_tls(self) -> None:
        self.state_machine.transition(StepId.TLS, StepStatus.RUNNING, message="Configuring HTTPS and reverse proxy...")
        self.paths.caddy_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        caddy_bin = self.paths.caddy_root / "bin" / "caddy"
        caddy_bin.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        if self._is_test_fixture():
            if not caddy_bin.exists():
                caddy_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            self.platform.set_executable_capability(caddy_bin, "cap_net_bind_service=+ep")
            self.state_machine.transition(StepId.TLS, StepStatus.SUCCESS, message="HTTPS and reverse proxy configured (mock).")
            return

        if not caddy_bin.exists():
            caddy_bin = self._download_and_verify_caddy()
        self.platform.set_executable_capability(caddy_bin, "cap_net_bind_service=+ep")

        caddyfile_content = self.generate_caddyfile(self._public_host, self.options.tls_mode)
        self.paths.caddyfile.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.paths.caddyfile.write_text(caddyfile_content, encoding="utf-8")

        if os.name != "nt" and getattr(self.platform, "os_name", "") == "linux" and shutil.which(str(caddy_bin)):
            val_res = subprocess.run(
                [str(caddy_bin), "validate", "--config", str(self.paths.caddyfile), "--adapter", "caddyfile"],
                capture_output=True,
                text=True,
            )
            if val_res.returncode != 0:
                raise InstallerError(f"Caddyfile validation failed: {val_res.stderr.strip()}")

        service_content = self.generate_caddy_service_unit()
        service_file = self.paths.unit_dest / "chief-tls-proxy.service"
        service_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        service_file.write_text(service_content, encoding="utf-8")
        try:
            service_file.chmod(0o600)
        except Exception:
            pass

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

        if self._is_test_fixture():
            self.platform.control_services(
                "start",
                ["chief-agent-dashboard.service", "chief-agent-worker.service", "chief-agent-scheduler.service", "chief-tls-proxy.service"],
            )
            self.state_machine.transition(StepId.HEALTH_CHECK, StepStatus.SUCCESS, message="All services active and healthy (mock).")
            return

        self.logger.info("Starting managed Chief services...")
        try:
            self.deployment_client.start_linux_services(self.paths.config)
        except Exception as exc:
            self.logger.warning(f"Native linux service start not available: {exc}. Delegating to platform adapter.")
            self.platform.control_services("start", [
                "chief-agent-dashboard.service", "chief-agent-worker.service", "chief-agent-scheduler.service"
            ])

        self.platform.control_services("restart", ["chief-tls-proxy.service"])

        for s in ("chief-agent-dashboard.service", "chief-agent-worker.service", "chief-agent-scheduler.service", "chief-tls-proxy.service"):
            if not self.platform.verify_service_active(s):
                raise InstallerError(f"Service {s} failed to become active")

        port = 8765
        if self.paths.config.exists():
            try:
                cfg_data = json.loads(self.paths.config.read_text(encoding="utf-8"))
                port = cfg_data.get("dashboard_port", 8765)
            except Exception:
                pass

        health_url = f"http://127.0.0.1:{port}/api/health"
        health_deadline_seconds = 15.0
        deadline = time.monotonic() + health_deadline_seconds
        healthy = False

        self.logger.info(
            f"Probing application health endpoint at {health_url} for up to "
            f"{health_deadline_seconds:.0f} seconds..."
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            probe_timeout = min(2.0, remaining)
            if self.platform.probe_health_endpoint(health_url, timeout=probe_timeout):
                healthy = True
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))

        if not healthy:
            raise InstallerError(
                f"Chief health endpoint at {health_url} failed to respond with 200 OK "
                f"within the {health_deadline_seconds:.0f}-second deadline."
            )

        self.state_machine.transition(StepId.HEALTH_CHECK, StepStatus.SUCCESS, message="All services active and healthy.")

    def _step_device_setup(self) -> None:
        self.state_machine.transition(StepId.DEVICE_SETUP, StepStatus.RUNNING, message="Exporting mobile setup certificates...")
        if self.options.tls_mode == "LOCAL":
            device_setup_dir = Path.home() / "Chief_Device_Setup"
            ca_root = self.paths.caddy_root / "data" / "caddy" / "pki" / "authorities" / "local" / "root.crt"
            if ca_root.exists():
                self.platform.export_device_ca(ca_root, device_setup_dir, self.options.owner_name, self._public_host or "chief.local")
            elif self._is_test_fixture():
                dummy_ca = self.paths.caddy_root / "root.crt"
                if not dummy_ca.exists():
                    dummy_ca.write_text("-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n", encoding="utf-8")
                self.platform.export_device_ca(dummy_ca, device_setup_dir, self.options.owner_name, self._public_host or "chief.local")
            else:
                for _ in range(20):
                    if ca_root.exists():
                        break
                    time.sleep(1)
                if not ca_root.exists():
                    raise InstallerError(f"Caddy local CA root certificate not found at {ca_root}")
                self.platform.export_device_ca(ca_root, device_setup_dir, self.options.owner_name, self._public_host or "chief.local")

        self.state_machine.transition(StepId.DEVICE_SETUP, StepStatus.SUCCESS, message="Mobile setup instructions exported.")
