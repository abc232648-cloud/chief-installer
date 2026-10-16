"""Security qualification and vulnerability resistance tests for Chief Unified Installer."""
import json
import os
from pathlib import Path
import pytest

from deployment.installer.errors import ReleaseMismatchError
from deployment.installer.logging import InstallerLogger, sanitize_text
from deployment.installer.manifest import ReleaseManifest
from deployment.installer.models import InstallOptions
from deployment.installer.platforms.linux import LinuxPlatformAdapter
from deployment.installer.privileged import linux as priv_linux


def test_sanitize_text_redacts_credentials():
    secret_cases = [
        ("password='SuperSecretPass123!'", "password='[REDACTED]'"),
        ('password="AnotherSecret456"', 'password="[REDACTED]"'),
        ("--password MySecretCLIValue", "--password [REDACTED]"),
        ("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz", "Bearer [REDACTED]"),
        ("token=ghp_1234567890abcdef", "token=[REDACTED]"),
        ("key:secret_api_key_value", "key:[REDACTED]"),
    ]
    for raw, expected in secret_cases:
        sanitized = sanitize_text(raw)
        assert sanitized == expected
        assert "SuperSecretPass123!" not in sanitized
        assert "AnotherSecret456" not in sanitized
        assert "MySecretCLIValue" not in sanitized


def test_passwords_never_in_log_files(tmp_path):
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_security_log")
    test_password = "P@ssw0rd_Super_Secret_2026_XYZ!"

    logger.info(f"User login attempt with password='{test_password}'")
    logger.warning(f"Failed retry with --password {test_password}")
    logger.error("Error occurred while verifying token", error=f"Context: password='{test_password}'")

    log_text = logger.log_file.read_text(encoding="utf-8")
    assert test_password not in log_text
    assert "[REDACTED]" in log_text

    events_text = logger.events_file.read_text(encoding="utf-8") if logger.events_file.exists() else ""
    assert test_password not in events_text


def test_install_options_string_representation_hides_password():
    secret = "TopSecretPassword999!"
    opts = InstallOptions(
        owner_name="Test Owner",
        owner_password=secret,
        farm_operations="ENABLED",
    )
    assert secret not in str(opts)
    assert secret not in repr(opts)
    assert "password_set=YES" in repr(opts)


def test_privileged_helper_command_injection_hardening():
    # Semicolon injection
    assert priv_linux.main(["helper", "install-packages; touch /tmp/pwned"]) == 2
    # Pipe injection
    assert priv_linux.main(["helper", "enable-linger", "user | cat /etc/shadow"]) == 2
    # Subshell injection
    assert priv_linux.main(["helper", "configure-avahi", "$(whoami)"]) == 2
    # Backtick injection
    assert priv_linux.main(["helper", "set-caddy-capability", "`id`"]) == 2
    # Newline injection
    assert priv_linux.main(["helper", "enable-linger", "user\nwhoami"]) == 2


def test_privileged_helper_package_whitelisting():
    # Only approved packages accepted
    assert priv_linux.run_install_packages(["gcc"]) == 2
    assert priv_linux.run_install_packages(["netcat"]) == 2
    assert priv_linux.run_install_packages(["sudo"]) == 2
    assert priv_linux.run_install_packages(["python3", "evil-pkg"]) == 2


def test_release_mismatch_blocks_execution():
    manifest = ReleaseManifest(
        release_id="chief-test-tampered",
        chief_agent_commit="819b7d76c9b17e0c58b50c32ea485991f163aeba",
        owner_pwa_commit="b8d139b0789918f3c937e640cc618e66194c966b",
        staff_pwa_commit="4a07d324e13630e2742a4e69127569d70a3cdf83",
        job_pwa_commit="90eb0cfa2155c4d5f6a4ca8bf510a5d01e1f7db9",
        node_version="22.23.3",
        caddy_version="2.11.7",
        caddy_checksum="5d27b76b95f496638d79208c55250317b8755fd9e90005f7466aa32956944a90",
        supported_installer_schema=1,
    )

    # Tampered commit
    with pytest.raises(ReleaseMismatchError) as exc:
        manifest.verify_component_commit("chief_agent", "1111111111111111111111111111111111111111")
    assert "commit mismatch" in str(exc.value)


def test_diagnostic_report_sanitization(tmp_path):
    logger = InstallerLogger(log_dir=tmp_path / "logs", session_id="test_diag_san")
    password = "SensitiveDiagnosticPassword123!"
    logger.info(f"Connecting to database with password='{password}'")

    report = logger.generate_diagnostic_report(
        failed_step="DATABASE_INIT",
        error=ValueError(f"Database connection failed with password='{password}'"),
    )

    assert password not in report
    assert "[REDACTED]" in report
    parsed = json.loads(report)
    assert parsed["failed_step"] == "DATABASE_INIT"
    assert parsed["error_type"] == "ValueError"
