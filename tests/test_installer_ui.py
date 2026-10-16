"""Headless unit tests for InstallerUIController wizard navigation, validation, and events."""
import time
import pytest

from deployment.installer.manifest import ACCEPTED_FIRST_RELEASE_MANIFEST
from deployment.installer.models import InstallResult, StepId
from deployment.installer.ui.controller import InstallerUIController, ScreenId
from tests.test_installer_orchestrator import FakePlatformAdapter


def test_ui_controller_clean_navigation_flow():
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)
    controller = InstallerUIController(platform_adapter=platform)

    # 1. Welcome screen
    assert controller.current_screen == ScreenId.WELCOME

    # 2. Transition to Preflight
    controller.next_from_welcome()
    assert controller.current_screen == ScreenId.PREFLIGHT
    time.sleep(0.05)  # Wait for async preflight worker

    # 3. Next to Identity (no existing install)
    controller.next_from_preflight()
    assert controller.current_screen == ScreenId.IDENTITY

    # 4. Identity validation
    controller.validate_identity("Jane Doe", "jane_admin")
    assert controller.owner_name == "Jane Doe"
    assert controller.owner_username == "jane_admin"
    assert controller.current_screen == ScreenId.PASSWORD

    # 5. Password validation
    controller.validate_password("SecurePassword2026!", "SecurePassword2026!")
    assert controller.current_screen == ScreenId.FARM_OPERATIONS

    # 6. Farm operations choice
    controller.validate_farm_operations(True, "Jane's Poultry", "East Coop")
    assert controller.farm_operations == "ENABLED"
    assert controller.organization_name == "Jane's Poultry"
    assert controller.farm_name == "East Coop"
    assert controller.current_screen == ScreenId.CONFIRMATION

    # 7. Summary verification (no passwords!)
    summary = controller.get_confirmation_summary()
    assert summary["Chief Owner"] == "Jane Doe"
    assert summary["Owner Username"] == "jane_admin"
    assert summary["Farm Operations"] == "Enabled"
    assert "SecurePassword2026!" not in str(summary)


def test_ui_controller_existing_install_flow():
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=True)
    controller = InstallerUIController(platform_adapter=platform)

    controller.next_from_welcome()
    time.sleep(0.05)

    # Should navigate to EXISTING_INSTALL screen
    controller.next_from_preflight()
    assert controller.current_screen == ScreenId.EXISTING_INSTALL

    # Refuses without explicit acknowledgement
    with pytest.raises(ValueError):
        controller.next_from_existing_install(acknowledged=False)

    # Acknowledging proceeds to IDENTITY
    controller.next_from_existing_install(acknowledged=True)
    assert controller.current_screen == ScreenId.IDENTITY


def test_ui_controller_identity_validation_failures():
    controller = InstallerUIController(platform_adapter=FakePlatformAdapter())

    # Empty name
    with pytest.raises(ValueError):
        controller.validate_identity("", "owner")

    # Name too long
    with pytest.raises(ValueError):
        controller.validate_identity("x" * 105, "owner")

    # Invalid username format
    with pytest.raises(ValueError):
        controller.validate_identity("Valid Name", "BAD USER!")


def test_ui_controller_password_validation_failures():
    controller = InstallerUIController(platform_adapter=FakePlatformAdapter())

    # Empty password
    with pytest.raises(ValueError):
        controller.validate_password("", "")

    # Too short
    with pytest.raises(ValueError):
        controller.validate_password("short", "short")

    # Mismatch
    with pytest.raises(ValueError):
        controller.validate_password("GoodPassword123!", "MismatchPassword123!")


def test_ui_controller_farm_validation_failures():
    controller = InstallerUIController(platform_adapter=FakePlatformAdapter())
    controller.owner_name = "Farm Owner"

    # Enabled without farm name
    with pytest.raises(ValueError):
        controller.validate_farm_operations(enabled=True, org_name="Org", farm_name="")


def test_ui_controller_execution_success():
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)

    # Fake orchestrator that immediately succeeds
    class FakeOrchestrator:
        def __init__(self, *args, **kwargs):
            self.current_step = StepId.COMPLETE
            self.logger = type("Logger", (), {"generate_diagnostic_report": lambda *a, **k: "{}"})()

        def execute_all(self):
            return InstallResult(
                status="SUCCESS",
                public_host="test.local",
                urls={"owner": "https://test.local/owner/"},
            )

    controller = InstallerUIController(
        platform_adapter=platform,
        orchestrator_factory=lambda opts: FakeOrchestrator(),
    )
    controller.owner_name = "Test"
    controller.owner_username = "owner"
    controller.owner_password = "SecretPassword!"

    controller.start_installation()
    time.sleep(0.05)

    assert controller.current_screen == ScreenId.COMPLETION
    assert controller.install_result.status == "SUCCESS"
    assert "https://test.local/owner/" in controller.install_result.urls["owner"]


def test_ui_controller_execution_failure():
    platform = FakePlatformAdapter(preflight_passes=True, has_existing_install=False)

    class FailingOrchestrator:
        def __init__(self, *args, **kwargs):
            self.current_step = StepId.SERVICES
            self.logger = type("Logger", (), {"generate_diagnostic_report": lambda *a, **k: '{"error": "Simulated failure"}'})()

        def execute_all(self):
            return InstallResult(
                status="FAILED",
                error="Simulated service failure",
                error_code="SERVICE_FAILED",
            )

    controller = InstallerUIController(
        platform_adapter=platform,
        orchestrator_factory=lambda opts: FailingOrchestrator(),
    )
    controller.owner_name = "Test"
    controller.owner_password = "SecretPassword!"

    controller.start_installation()
    time.sleep(0.05)

    assert controller.current_screen == ScreenId.FAILURE
    assert "Simulated service failure" in controller.failed_reason
    assert "Simulated failure" in controller.diagnostic_report
