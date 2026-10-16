"""Tests for installer compatibility contract client and Chief Agent boundary."""
from pathlib import Path
import pytest

from deployment.installer import parse_manifest
from deployment.installer.contract import (
    ChiefAgentDeploymentClient,
    ContractCompatibilityError,
    INSTALLER_CONTRACT_VERSION,
    REQUIRED_AGENT_CONTRACT_VERSION,
)


def test_contract_versions():
    assert INSTALLER_CONTRACT_VERSION == "1.0.0"
    assert REQUIRED_AGENT_CONTRACT_VERSION == "1.0.0"


def test_local_host_label_derivation():
    client = ChiefAgentDeploymentClient()
    label1 = client.derive_local_host_label("Green Farm")
    assert label1 == "chief-green-farm"

    label2 = client.derive_local_host_label("Apex & Co. Agricultural Services Ltd. Long Name")
    assert label2.startswith("chief-apex-co-agricultural-services")
    assert len(label2) <= 63

    label3 = client.derive_local_host_label("   ")
    assert label3.startswith("chief-owner-")


def test_missing_agent_root_fails_closed():
    client = ChiefAgentDeploymentClient()
    with pytest.raises(ContractCompatibilityError) as exc_info:
        client.verify_compatibility(agent_root=None)
    assert "was not specified" in str(exc_info.value)


def test_nonexistent_directory_fails_closed(tmp_path):
    missing_dir = tmp_path / "does_not_exist"
    client = ChiefAgentDeploymentClient()
    with pytest.raises(ContractCompatibilityError) as exc_info:
        client.verify_compatibility(agent_root=missing_dir)
    assert "does not exist" in str(exc_info.value)


def test_missing_contract_file_fails_closed(tmp_path):
    client = ChiefAgentDeploymentClient()
    with pytest.raises(ContractCompatibilityError) as exc_info:
        client.verify_compatibility(agent_root=tmp_path)
    assert "missing 'deployment/contract.py'" in str(exc_info.value)


def test_explicit_test_mock_handshake():
    client = ChiefAgentDeploymentClient()
    handshake = client.verify_compatibility(agent_root=None, allow_test_mock=True)
    assert handshake.is_compatible is True
    assert handshake.contract_version == "test-mock-1.0.0"
    assert "prepare_instance" in handshake.supported_capabilities


def test_actual_chief_agent_handshake():
    agent_root = Path(__file__).resolve().parents[2] / "chief-agent"
    if (agent_root / "deployment" / "contract.py").exists():
        client = ChiefAgentDeploymentClient(agent_root=agent_root)
        handshake = client.verify_compatibility()
        assert handshake.is_compatible is True
        review_manifest = parse_manifest(Path(__file__).resolve().parents[1] / "review-dns-manifest.json")
        assert handshake.chief_agent_commit == review_manifest.chief_agent_commit


def test_incompatible_version_raises(tmp_path):
    contract_file = tmp_path / "deployment" / "contract.py"
    contract_file.parent.mkdir(parents=True)
    contract_file.write_text(
        'import sys, json\n'
        'if "--info" in sys.argv:\n'
        '    print(json.dumps({"contract_version": "2.0.0", "chief_agent_commit": "abc", "supported_capabilities": ["prepare_instance", "bootstrap_install", "install_readiness"]}))\n',
        encoding="utf-8",
    )

    client = ChiefAgentDeploymentClient(agent_root=tmp_path)
    with pytest.raises(ContractCompatibilityError) as exc_info:
        client.verify_compatibility(agent_root=tmp_path)
    assert "Incompatible Chief Agent contract version" in str(exc_info.value)


def test_missing_capabilities_raises(tmp_path):
    contract_file = tmp_path / "deployment" / "contract.py"
    contract_file.parent.mkdir(parents=True)
    contract_file.write_text(
        'import sys, json\n'
        'if "--info" in sys.argv:\n'
        '    print(json.dumps({"contract_version": "1.0.0", "chief_agent_commit": "abc", "supported_capabilities": ["other_cap"]}))\n',
        encoding="utf-8",
    )

    client = ChiefAgentDeploymentClient(agent_root=tmp_path)
    with pytest.raises(ContractCompatibilityError) as exc_info:
        client.verify_compatibility(agent_root=tmp_path)
    assert "missing required capabilities" in str(exc_info.value)
