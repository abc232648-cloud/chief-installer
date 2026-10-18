"""Chief Installer -> Chief Agent Deployment Compatibility Contract.

Handles version negotiation and invocation of Chief Agent's deployment
capabilities (production preparation, managed install, readiness, backup,
service unit generation, and manifest verification) across versioned boundaries.
Strictly fails closed in production when the agent contract is missing or invalid.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from .process import run as run_quiet, popen as popen_quiet
import sys
from typing import Any, Dict, List, Optional
import unicodedata

INSTALLER_CONTRACT_VERSION = "1.0.0"
REQUIRED_AGENT_CONTRACT_VERSION = "1.0.0"


class ContractCompatibilityError(Exception):
    """Raised when Chief Agent deployment contract is missing, invalid, or incompatible."""
    code = "CONTRACT_COMPATIBILITY_MISMATCH"


@dataclass(frozen=True)
class ContractHandshake:
    agent_root: Path
    contract_version: str
    chief_agent_commit: str
    supported_capabilities: List[str]
    is_compatible: bool


class ChiefAgentDeploymentClient:
    """Client for interacting with Chief Agent deployment operations.
    
    Enforces fail-closed validation of the target Chief Agent deployment contract.
    """

    def __init__(self, agent_root: Optional[Path] = None, python_bin: Optional[Path] = None):
        self.agent_root = agent_root
        self.python_bin = python_bin or Path(sys.executable)
        self._handshake: Optional[ContractHandshake] = None

    def verify_compatibility(
        self,
        agent_root: Optional[Path] = None,
        *,
        expected_commit: Optional[str] = None,
        allow_test_mock: bool = False,
    ) -> ContractHandshake:
        """Verify that target Chief Agent checkout satisfies the contract requirements.
        
        Fails closed by default. Mock compatibility is strictly rejected unless
        explicitly requested via allow_test_mock=True. If expected_commit is provided,
        enforces that the reported chief_agent_commit matches the manifest.
        """
        root = agent_root or self.agent_root
        if root is None:
            if allow_test_mock:
                return self._create_test_mock_handshake(Path("."))
            raise ContractCompatibilityError("Chief Agent root directory was not specified.")

        if not root.exists():
            if allow_test_mock:
                return self._create_test_mock_handshake(root)
            raise ContractCompatibilityError(f"Chief Agent directory does not exist: {root}")

        contract_file = root / "deployment" / "contract.py"
        if not contract_file.exists():
            if allow_test_mock:
                return self._create_test_mock_handshake(root)
            raise ContractCompatibilityError(
                f"Target Chief Agent checkout at '{root}' is missing 'deployment/contract.py'. "
                f"The target checkout does not implement the required deployment contract."
            )

        python_exec = str(self.python_bin) if self.python_bin.exists() else sys.executable
        cmd = [python_exec, "-m", "deployment.contract", "--info"]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(root) + (f"{os.pathsep}{env['PYTHONPATH']}" if "PYTHONPATH" in env else "")

        try:
            res = run_quiet(
                cmd,
                cwd=str(root),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                raise ContractCompatibilityError(
                    f"Deployment contract interrogation failed with exit code {res.returncode}: {res.stderr.strip()}"
                )
            info = json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            raise ContractCompatibilityError(
                f"Deployment contract at {root} returned invalid JSON: {exc}"
            ) from exc
        except Exception as exc:
            if isinstance(exc, ContractCompatibilityError):
                raise
            raise ContractCompatibilityError(
                f"Failed to query deployment contract info from {root}: {exc}"
            ) from exc

        agent_version = info.get("contract_version", "")
        if not agent_version or not agent_version.startswith("1."):
            raise ContractCompatibilityError(
                f"Incompatible Chief Agent contract version: '{agent_version}' "
                f"(required: ^{REQUIRED_AGENT_CONTRACT_VERSION})"
            )

        capabilities = info.get("supported_capabilities", [])
        required_caps = ["prepare_instance", "bootstrap_install", "install_readiness", "chief_scoped_dns"]
        missing_caps = [c for c in required_caps if c not in capabilities]
        if missing_caps:
            raise ContractCompatibilityError(
                f"Chief Agent deployment contract is missing required capabilities: {missing_caps}"
            )

        reported_commit = str(info.get("chief_agent_commit", "")).strip().lower()
        if expected_commit and not allow_test_mock:
            if reported_commit != expected_commit.strip().lower():
                raise ContractCompatibilityError(
                    f"Chief Agent commit mismatch: deployment contract reported '{reported_commit}', "
                    f"expected pinned release commit '{expected_commit}'."
                )

        handshake = ContractHandshake(
            agent_root=root,
            contract_version=agent_version,
            chief_agent_commit=reported_commit,
            supported_capabilities=capabilities,
            is_compatible=True,
        )
        self._handshake = handshake
        return handshake

    def _create_test_mock_handshake(self, root: Path) -> ContractHandshake:
        """Explicit test-only mock handshake."""
        handshake = ContractHandshake(
            agent_root=root,
            contract_version="test-mock-1.0.0",
            chief_agent_commit="a9a1f33e42f1cfc7c73811ffd13ae3db0963cb47",
            supported_capabilities=[
                "chief_scoped_dns",
                "prepare_instance",
                "source_manifest",
                "bootstrap_install",
                "install_readiness",
                "install_backup",
                "service_files",
                "linux_service_control",
                "ownership_bootstrap",
            ],
            is_compatible=True,
        )
        self._handshake = handshake
        return handshake

    def derive_local_host_label(self, owner_name: str) -> str:
        """Derive standard RFC 1035 host label for local mDNS discovery."""
        clean = " ".join(owner_name.strip().split())
        ascii_name = unicodedata.normalize("NFKD", clean).encode("ascii", "ignore").decode("ascii")
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
        if not slug:
            slug = "owner-" + hashlib.sha256(clean.encode("utf-8")).hexdigest()[:10]
        return f"chief-{slug[:57].rstrip('-')}"

    def _invoke_contract(self, func_name: str, **kwargs) -> Any:
        """Execute a capability wrapper function inside Chief Agent via subprocess over stdin."""
        root = self.agent_root
        if not root or not root.exists():
            raise ContractCompatibilityError(f"Chief Agent root does not exist: {root}")

        runner = (
            "import sys, json\n"
            "from pathlib import Path\n"
            "from deployment import contract\n"
            "raw = sys.stdin.read()\n"
            "kwargs = json.loads(raw)\n"
            f"func_name = '{func_name}'\n"
            "path_keys = {'source_root', 'output', 'root', 'config', 'source_manifest', "
            "'browser_runtime', 'destination', 'directory', 'python', 'instance_config', "
            "'backup_directory', 'managed_installation', 'target_dir'}\n"
            "if func_name == 'write_service_definitions':\n"
            "    path_keys = {'target_dir'}\n"
            "converted = {}\n"
            "for k, v in kwargs.items():\n"
            "    if k in path_keys and v is not None:\n"
            "        converted[k] = Path(v)\n"
            "    else:\n"
            "        converted[k] = v\n"
            f"func = getattr(contract, '{func_name}')\n"
            "res = func(**converted)\n"
            "if isinstance(res, list):\n"
            "    res = [str(x) for x in res]\n"
            "print(json.dumps(res))\n"
        )
        python_exec = str(self.python_bin) if self.python_bin.exists() else sys.executable
        cmd = [python_exec, "-c", runner]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(root) + (f"{os.pathsep}{env['PYTHONPATH']}" if "PYTHONPATH" in env else "")

        serializable_kwargs = {k: str(v) if isinstance(v, Path) else v for k, v in kwargs.items()}
        payload = json.dumps(serializable_kwargs)

        proc = popen_quiet(
            cmd,
            cwd=str(root),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate(input=payload)
        if proc.returncode != 0:
            raise ContractCompatibilityError(
                f"Contract capability '{func_name}' failed with exit code {proc.returncode}: {stderr.strip()}"
            )
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ContractCompatibilityError(
                f"Contract capability '{func_name}' returned invalid JSON: {stdout[:200]}"
            ) from exc

    def build_source_manifest(
        self,
        source_root: Path,
        output: Path,
        expected_git_commit: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute source manifest generation."""
        return self._invoke_contract(
            "build_source_manifest",
            source_root=source_root,
            output=output,
            expected_git_commit=expected_git_commit,
        )

    def prepare_production(
        self,
        root: Path,
        config: Path,
        source_root: Path,
        source_manifest: Path,
        username: str,
        password: str,
        *,
        public_host: str,
        browser_runtime: Path,
        dashboard_port: int = 8765,
        trusted_tls_proxy: str = "127.0.0.1",
        farm_operations: str = "DISABLED",
        organization_name: Optional[str] = None,
        farm_name: Optional[str] = None,
        dns_mode: str = "quad9",
    ) -> Dict[str, Any]:
        """Execute production instance preparation."""
        return self._invoke_contract(
            "prepare_production",
            root=root,
            config=config,
            source_root=source_root,
            source_manifest=source_manifest,
            username=username,
            password=password,
            public_host=public_host,
            browser_runtime=browser_runtime,
            dashboard_port=dashboard_port,
            trusted_tls_proxy=trusted_tls_proxy,
            farm_operations=farm_operations,
            organization_name=organization_name,
            farm_name=farm_name,
            dns_mode=dns_mode,
        )

    def create_install_backup(
        self,
        config: Path,
        destination: Path,
        *,
        source_root: Path,
        source_manifest: Path,
        private_storage_confirmed: bool = True,
    ) -> Dict[str, Any]:
        """Execute initial install backup."""
        return self._invoke_contract(
            "create_install_backup",
            config=config,
            destination=destination,
            source_root=source_root,
            source_manifest=source_manifest,
            private_storage_confirmed=private_storage_confirmed,
        )

    def bootstrap_installation(
        self,
        directory: Path,
        *,
        source_root: Path,
        source_manifest: Path,
        expected_source: str,
        python: Path,
        instance_config: Path,
        schema_sha256: str,
        private_storage_confirmed: bool = True,
    ) -> Dict[str, Any]:
        """Execute managed installation bootstrap."""
        return self._invoke_contract(
            "bootstrap_installation",
            directory=directory,
            source_root=source_root,
            source_manifest=source_manifest,
            expected_source=expected_source,
            python=python,
            instance_config=instance_config,
            schema_sha256=schema_sha256,
            private_storage_confirmed=private_storage_confirmed,
        )

    def installation_readiness(
        self,
        config: Path,
        *,
        source_root: Path,
        source_manifest: Path,
        backup_directory: Path,
        manifest_sha256: str,
        expected_source: str,
        expected_schema: str,
        platform: str,
        project: str,
        user: str,
        python: Optional[Path] = None,
        managed_installation: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Execute installation readiness gate validation."""
        return self._invoke_contract(
            "installation_readiness",
            config=config,
            source_root=source_root,
            source_manifest=source_manifest,
            backup_directory=backup_directory,
            manifest_sha256=manifest_sha256,
            expected_source=expected_source,
            expected_schema=expected_schema,
            platform=platform,
            project=project,
            user=user,
            python=python,
            managed_installation=managed_installation,
        )

    def write_service_definitions(
        self,
        target_dir: Path,
        *,
        platform: str,
        project: str,
        user: str,
        instance_config: str,
        python: Optional[str] = None,
        managed_installation: Optional[str] = None,
    ) -> List[Path]:
        """Generate and write service definition files."""
        result = self._invoke_contract(
            "write_service_definitions",
            target_dir=target_dir,
            platform=platform,
            project=project,
            user=user,
            instance_config=instance_config,
            python=python,
            managed_installation=managed_installation,
        )
        return [Path(p) for p in result]

    def stop_linux_services(self, timeout: int = 60) -> Dict[str, Any]:
        """Stop running Chief user services."""
        return self._invoke_contract("stop_linux_services", timeout=timeout)

    def start_linux_services(self, config_path: Path, timeout: int = 90) -> Dict[str, Any]:
        """Start managed Chief user services and verify READY status."""
        root = self.agent_root
        runner = (
            "import sys, json\n"
            "from deployment.launch import configure\n"
            "from deployment.linux_service_control import start_services\n"
            "instance = configure(sys.argv[1])\n"
            "res = start_services(instance, timeout=int(sys.argv[2]))\n"
            "print(json.dumps(res))\n"
        )
        cmd = [str(self.python_bin), "-c", runner, str(config_path), str(timeout)]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(root) + (f"{os.pathsep}{env['PYTHONPATH']}" if "PYTHONPATH" in env else "")
        proc = run_quiet(cmd, cwd=str(root), env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            raise ContractCompatibilityError(f"Starting services failed: {proc.stderr.strip()}")
        return json.loads(proc.stdout)
