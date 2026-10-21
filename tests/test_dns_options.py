import json
from pathlib import Path
import pytest
from deployment.installer.models import InstallOptions
from deployment.installer.contract import ChiefAgentDeploymentClient, ContractCompatibilityError


def test_default_and_system_options():
    assert InstallOptions(owner_name='Owner').dns_mode == 'quad9'
    assert InstallOptions(owner_name='Owner', dns_mode='system').dns_mode == 'system'
    with pytest.raises(ValueError):
        InstallOptions(owner_name='Owner', dns_mode='automatic')


def test_dns_selection_crosses_contract_boundary(monkeypatch):
    client = ChiefAgentDeploymentClient()
    captured = {}
    monkeypatch.setattr(client, '_invoke_contract', lambda action, **kwargs: captured.update(kwargs) or {})
    client.prepare_production(Path('/state'), Path('/config'), Path('/source'), Path('/manifest'),
                              'owner', 'password', public_host='chief.example',
                              browser_runtime=Path('/browser'), dns_mode='system')
    assert captured['dns_mode'] == 'system'


def test_old_agent_without_dns_support_is_refused(tmp_path):
    contract = tmp_path / 'deployment' / 'contract.py'
    contract.parent.mkdir()
    contract.write_text('import json\nprint(json.dumps(' + repr({
        'contract_version': '1.0.0', 'chief_agent_commit': 'a' * 40,
        'supported_capabilities': ['prepare_instance', 'bootstrap_install', 'install_readiness']}) + '))\n')
    with pytest.raises(ContractCompatibilityError, match='chief_scoped_dns'):
        ChiefAgentDeploymentClient(agent_root=tmp_path).verify_compatibility()


def test_gui_summary_preserves_selection():
    from deployment.installer.ui.controller import InstallerUIController
    controller = InstallerUIController(platform_adapter=None)
    assert controller.get_confirmation_summary()['Chief DNS'] == 'Quad9 Secure DoH'
    controller.dns_mode = 'system'
    assert controller.get_confirmation_summary()['Chief DNS'] == 'System DNS'
