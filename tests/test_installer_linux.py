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


def test_linux_adapter_ignores_current_run_staging_and_logs(tmp_path, monkeypatch):
    """Verified source staging and current diagnostics must not self-trigger archive."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    adapter = LinuxPlatformAdapter()

    share_root = tmp_path / ".local" / "share" / "chief"
    tooling = share_root / "tooling"
    release_source = share_root / "release-source"
    tooling.mkdir(parents=True)
    release_source.mkdir(parents=True)
    (tooling / "requirements.txt").write_text("playwright==1.63.0\n", encoding="utf-8")
    (release_source / "README.md").write_text("verified source", encoding="utf-8")

    log_dir = tmp_path / ".local" / "state" / "chief" / "installer" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "installer_current.log").write_text("current run", encoding="utf-8")

    has_existing, paths = adapter.detect_existing_installation()
    assert not has_existing
    assert paths == []


def test_linux_adapter_detects_partial_runtime_from_failed_install(tmp_path, monkeypatch):
    """A venv left by a failed run is installed state and must be recycled."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    adapter = LinuxPlatformAdapter()

    runtime_python = tmp_path / ".local" / "share" / "chief" / "runtime" / "bin" / "python"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("partial runtime", encoding="utf-8")

    has_existing, paths = adapter.detect_existing_installation()
    assert has_existing
    assert str(tmp_path / ".local" / "share" / "chief") in paths


def test_linux_adapter_detects_authoritative_existing_installation(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    adapter = LinuxPlatformAdapter()

    source_manifest = tmp_path / ".local" / "state" / "chief" / "release-metadata" / "SOURCE_MANIFEST.json"
    source_manifest.parent.mkdir(parents=True)
    source_manifest.write_text("{}", encoding="utf-8")

    has_existing, paths = adapter.detect_existing_installation()
    assert has_existing
    assert str(tmp_path / ".local" / "state" / "chief") in paths


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

    share_root = tmp_path / ".local" / "share" / "chief"
    tooling = share_root / "tooling"
    release_source = share_root / "release-source"
    runtime = share_root / "runtime"
    tooling.mkdir(parents=True)
    release_source.mkdir(parents=True)
    runtime.mkdir(parents=True)
    (tooling / "requirements.txt").write_text("playwright==1.63.0\n", encoding="utf-8")
    (release_source / "README.md").write_text("new verified source", encoding="utf-8")
    (runtime / "partial.txt").write_text("old partial runtime", encoding="utf-8")

    state_root = tmp_path / ".local" / "state" / "chief"
    release_meta = state_root / "release-metadata"
    release_meta.mkdir(parents=True)
    (release_meta / "SOURCE_MANIFEST.json").write_text("old manifest", encoding="utf-8")
    current_logs = state_root / "installer" / "logs"
    current_logs.mkdir(parents=True)
    (current_logs / "current.log").write_text("keep current diagnostic", encoding="utf-8")
    old_db = state_root / "state.sqlite3"
    old_db.write_text("old db", encoding="utf-8")

    config_root = tmp_path / ".config" / "chief"
    config_root.mkdir(parents=True)
    (config_root / "production-instance.json").write_text("{}", encoding="utf-8")

    archive_root = tmp_path / "Chief_Previous_Installations"
    arch_dir = adapter.archive_existing_installation(archive_root)

    assert arch_dir.exists()
    assert old_agent.exists()
    assert (old_agent / "file.txt").read_text(encoding="utf-8") == "important source data"

    # Fresh verified source and the active diagnostic log survive the archive.
    assert tooling.exists()
    assert release_source.exists()
    assert (tooling / "requirements.txt").exists()
    assert (release_source / "README.md").exists()
    assert (current_logs / "current.log").read_text(encoding="utf-8") == "keep current diagnostic"

    # Prior/partial installed state is archived.
    assert not runtime.exists()
    assert not release_meta.exists()
    assert not old_db.exists()
    assert not config_root.exists()
    assert (arch_dir / "share" / "runtime" / "partial.txt").read_text(encoding="utf-8") == "old partial runtime"
    assert (arch_dir / "state" / "release-metadata" / "SOURCE_MANIFEST.json").read_text(encoding="utf-8") == "old manifest"
    assert (arch_dir / "state" / "state.sqlite3").read_text(encoding="utf-8") == "old db"
    assert (arch_dir / "config" / "chief" / "production-instance.json").exists()

    summary_file = arch_dir / "ARCHIVE_SUMMARY.txt"
    assert summary_file.exists()
    summary = summary_file.read_text(encoding="utf-8")
    assert "Nothing was deleted" in summary
    assert "Current-run verified source staging" in summary
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
