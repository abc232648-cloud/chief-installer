"""Tests for Linux platform adapter and constrained privileged helper."""
import os
from pathlib import Path
import subprocess
import sys
import pytest

from deployment.installer.errors import PrivilegeError
from deployment.installer.platforms.linux import LinuxPlatformAdapter
from deployment.installer.privileged import linux as priv_linux
import deployment.installer.platforms.linux as linux_platform


def test_privileged_helper_rejects_unauthorized_packages():
    # Only strictly whitelisted packages are accepted
    assert priv_linux.run_install_packages(["evil-package"]) == 2
    assert priv_linux.run_install_packages(["curl", "nmap"]) == 2


def test_privileged_helper_rejects_malformed_linger_username():
    assert priv_linux.run_enable_linger("user; rm -rf /") == 2
    assert priv_linux.run_enable_linger("bad$user*") == 2
    assert priv_linux.run_enable_linger("") == 2


def test_privileged_helper_rejects_malformed_avahi_label():
    assert priv_linux.run_configure_avahi("-bad-start") == 2
    assert priv_linux.run_configure_avahi("label with spaces") == 2
    assert priv_linux.run_configure_avahi("label;evil") == 2


def test_privileged_helper_rejects_invalid_caddy_capability(tmp_path):
    # Non-existent path
    assert priv_linux.run_set_caddy_capability(str(tmp_path / "missing_caddy")) == 2

    # Non-caddy binary name
    fake_bin = tmp_path / "not_caddy"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    assert priv_linux.run_set_caddy_capability(str(fake_bin)) == 2


def test_privileged_helper_cli_rejections():
    # Unknown commands
    assert priv_linux.main(["helper", "arbitrary-command"]) == 2
    assert priv_linux.main(["helper"]) == 2
    assert priv_linux.main(["helper", "enable-linger"]) == 2  # missing arg


def test_frozen_helper_reenters_packaged_executable(tmp_path, monkeypatch):
    """A PyInstaller build must never invoke itself as `python -m ...`."""
    frozen_executable = tmp_path / "chief-installer"
    frozen_executable.write_text("binary-placeholder", encoding="utf-8")

    monkeypatch.setattr(linux_platform.sys, "frozen", True, raising=False)
    monkeypatch.setenv("CHIEF_INSTALLER_FROZEN_EXECUTABLE", str(frozen_executable))

    def fake_which(name):
        if name == "pkexec":
            return "/usr/bin/pkexec"
        return None

    monkeypatch.setattr(linux_platform.shutil, "which", fake_which)

    adapter = LinuxPlatformAdapter()
    assert adapter._get_helper_prefix() == [
        "pkexec",
        str(frozen_executable),
        "--internal-privileged-helper",
    ]


def test_linux_adapter_ignores_developer_trees_as_installs(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    adapter = LinuxPlatformAdapter()

    # Developer/source/runtime trees alone are not installed Chief state.
    (tmp_path / "chief-agent" / ".git").mkdir(parents=True)
    (tmp_path / "chief-runtime").mkdir(parents=True)
    (tmp_path / "chief-release-review").mkdir(parents=True)

    has_existing, paths = adapter.detect_existing_installation()
    assert not has_existing
    assert paths == []


def test_linux_adapter_detects_authoritative_existing_installation(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    adapter = LinuxPlatformAdapter()

    old_state = tmp_path / ".local" / "state" / "chief"
    old_state.mkdir(parents=True)

    has_existing, paths = adapter.detect_existing_installation()
    assert has_existing
    assert str(old_state) in paths


def test_linux_adapter_detects_old_service_definition(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    adapter = LinuxPlatformAdapter()

    unit = tmp_path / ".config" / "systemd" / "user" / "chief-agent-worker.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/old/chief\n", encoding="utf-8")

    has_existing, paths = adapter.detect_existing_installation()
    assert has_existing
    assert str(unit) in paths


def test_linux_adapter_archive_moves_installed_state_but_preserves_source(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    adapter = LinuxPlatformAdapter()

    # Development source must survive a production install/archive operation.
    old_agent = tmp_path / "chief-agent"
    old_agent.mkdir(parents=True)
    (old_agent / "file.txt").write_text("important source data", encoding="utf-8")

    # Authoritative installed state must be archived, never deleted.
    old_state = tmp_path / ".local" / "state" / "chief"
    old_state.mkdir(parents=True)
    (old_state / "state.sqlite3").write_text("old db", encoding="utf-8")

    archive_root = tmp_path / "Chief_Previous_Installations"
    arch_dir = adapter.archive_existing_installation(archive_root)

    assert arch_dir.exists()
    assert old_agent.exists()
    assert (old_agent / "file.txt").read_text(encoding="utf-8") == "important source data"
    assert not old_state.exists()

    archived_state = arch_dir / "state" / "chief" / "state.sqlite3"
    assert archived_state.exists()
    assert archived_state.read_text(encoding="utf-8") == "old db"

    summary_file = arch_dir / "ARCHIVE_SUMMARY.txt"
    assert summary_file.exists()
    summary = summary_file.read_text(encoding="utf-8")
    assert "Nothing was deleted" in summary
    assert "Development/source directories" in summary


def test_linux_adapter_device_setup_export(tmp_path):
    adapter = LinuxPlatformAdapter()
    ca_cert = tmp_path / "test_root.crt"
    ca_cert.write_text("CERT_DATA", encoding="utf-8")
    dest_dir = tmp_path / "Chief_Device_Setup"

    readme = adapter.export_device_ca(ca_cert, dest_dir, "Jane Doe", "jane-chief.local")

    assert readme.exists()
    readme_text = readme.read_text(encoding="utf-8")
    assert "Chief device setup for Jane Doe" in readme_text
    assert "https://jane-chief.local/owner/" in readme_text
    assert (dest_dir / "Chief-Local-CA.crt").exists()
    assert (dest_dir / "Chief-Local-CA.crt").read_text(encoding="utf-8") == "CERT_DATA"
