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
        allow_test_mock: bool = False,
    ) -> ContractHandshake:
        """Verify that target Chief Agent checkout satisfies the contract requirements.
        
        Fails closed by default. Mock compatibility is strictly rejected unless
        explicitly requested via allow_test_mock=True.
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

        cmd = [str(self.python_bin), "-m", "deployment.contract", "--info"]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(root) + (f"{os.pathsep}{env['PYTHONPATH']}" if "PYTHONPATH" in env else "")

        try:
            res = subprocess.run(
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
        required_caps = ["prepare_instance", "bootstrap_install", "install_readiness"]
        missing_caps = [c for c in required_caps if c not in capabilities]
        if missing_caps:
            raise ContractCompatibilityError(
                f"Chief Agent deployment contract is missing required capabilities: {missing_caps}"
            )

        handshake = ContractHandshake(
            agent_root=root,
            contract_version=agent_version,
            chief_agent_commit=info.get("chief_agent_commit", ""),
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
            chief_agent_commit="819b7d76c9b17e0c58b50c32ea485991f163aeba",
            supported_capabilities=[
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
