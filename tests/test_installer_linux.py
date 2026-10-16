"""Tests for Linux platform adapter and constrained privileged helper."""
import os
from pathlib import Path
import subprocess
import sys
import pytest

from deployment.installer.errors import PrivilegeError
from deployment.installer.platforms.linux import LinuxPlatformAdapter
from deployment.installer.privileged import linux as priv_linux


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


def test_linux_adapter_detects_existing_installations(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    adapter = LinuxPlatformAdapter()

    has_existing, paths = adapter.detect_existing_installation()
    assert not has_existing
    assert len(paths) == 0

    # Create fake existing directory
    (tmp_path / "chief-agent").mkdir(parents=True)
    has_existing2, paths2 = adapter.detect_existing_installation()
    assert has_existing2
    assert str(tmp_path / "chief-agent") in paths2


def test_linux_adapter_archive_moves_paths_without_deletion(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    adapter = LinuxPlatformAdapter()

    # Create dummy previous directories
    old_agent = tmp_path / "chief-agent"
    old_agent.mkdir(parents=True)
    (old_agent / "file.txt").write_text("important old data", encoding="utf-8")

    old_state = tmp_path / ".local" / "state" / "chief"
    old_state.mkdir(parents=True)
    (old_state / "state.sqlite3").write_text("old db", encoding="utf-8")

    archive_root = tmp_path / "Chief_Previous_Installations"
    arch_dir = adapter.archive_existing_installation(archive_root)

    assert arch_dir.exists()
    assert not old_agent.exists()  # Moved, not deleted
    assert not old_state.exists()

    # Verify moved files exist in archive
    archived_file = arch_dir / "home" / "chief-agent" / "file.txt"
    assert archived_file.exists()
    assert archived_file.read_text(encoding="utf-8") == "important old data"

    archived_state = arch_dir / "state" / "chief" / "state.sqlite3"
    assert archived_state.exists()
    assert archived_state.read_text(encoding="utf-8") == "old db"

    summary_file = arch_dir / "ARCHIVE_SUMMARY.txt"
    assert summary_file.exists()
    assert "Nothing was deleted" in summary_file.read_text(encoding="utf-8")


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
