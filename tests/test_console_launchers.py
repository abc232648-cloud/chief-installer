"""Tests for Chief Console desktop launcher and shortcut integration across platforms."""
from pathlib import Path
import pytest

from deployment.installer.platforms.linux import LinuxPlatformAdapter
from deployment.installer.platforms.windows import WindowsPlatformAdapter
from deployment.installer.orchestrator import InstallerPaths


def test_linux_creates_distinct_console_and_installer_desktop_entries(tmp_path, monkeypatch):
    home = tmp_path / "home"
    desktop = home / "Desktop"
    desktop.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)

    adapter = LinuxPlatformAdapter()
    paths = InstallerPaths(
        root=home / "chief",
        tooling=home / "tooling",
        release_source=home / "release_source",
        runtime=home / "runtime",
        python=home / "python",
        node_root=home / "node",
        pwa_root=home / "pwa",
        managed=home / "managed",
        production=home / "production",
        caddy_root=home / "caddy",
        config_root=home / "config",
        config=home / "config" / "production-instance.json",
        identity_config=home / "config" / "installation-identity.json",
        browser_config=home / "config" / "browser.json",
        caddyfile=home / "config" / "Caddyfile",
        state_root=home / "state",
        meta=home / "meta",
        source_manifest=home / "manifest.json",
        backup=home / "backup",
        unit_stage=home / "unit_stage",
        unit_dest=home / "unit_dest",
        archive_base=home / "archive",
    )

    adapter.create_console_launchers(paths, "chief-bigi.local")

    apps_dir = home / ".local" / "share" / "applications"
    console_entry = apps_dir / "chief-console.desktop"
    installer_entry = apps_dir / "chief-installer.desktop"

    assert console_entry.exists()
    assert installer_entry.exists()

    console_content = console_entry.read_text(encoding="utf-8")
    assert "Name=Chief Console" in console_content
    assert "--console" in console_content
    assert "StartupWMClass=ChiefConsole" in console_content

    installer_content = installer_entry.read_text(encoding="utf-8")
    assert "Name=Chief Installer" in installer_content
    assert "--gui" in installer_content

    # Desktop folder also gets both shortcuts
    assert (desktop / "chief-console.desktop").exists()
    assert (desktop / "chief-installer.desktop").exists()


def test_windows_creates_distinct_console_and_installer_shortcuts(tmp_path, monkeypatch):
    home = tmp_path / "home"
    desktop = home / "Desktop"
    desktop.mkdir(parents=True)
    appdata = home / "AppData" / "Roaming"
    appdata.mkdir(parents=True)

    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(Path, "home", lambda: home)

    captured_cmds = []
    import subprocess
    def mock_run(cmd, *args, **kwargs):
        captured_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(subprocess, "run", mock_run)

    adapter = WindowsPlatformAdapter()
    paths = InstallerPaths(
        root=home / "chief",
        tooling=home / "tooling",
        release_source=home / "release_source",
        runtime=home / "runtime",
        python=home / "python",
        node_root=home / "node",
        pwa_root=home / "pwa",
        managed=home / "managed",
        production=home / "production",
        caddy_root=home / "caddy",
        config_root=home / "config",
        config=home / "config" / "production-instance.json",
        identity_config=home / "config" / "installation-identity.json",
        browser_config=home / "config" / "browser.json",
        caddyfile=home / "config" / "Caddyfile",
        state_root=home / "state",
        meta=home / "meta",
        source_manifest=home / "manifest.json",
        backup=home / "backup",
        unit_stage=home / "unit_stage",
        unit_dest=home / "unit_dest",
        archive_base=home / "archive",
    )

    adapter.create_console_launchers(paths, "chief-bigi.local")

    # Verify PowerShell WScript.Shell was called to create shortcuts
    assert len(captured_cmds) >= 2
    cmd_texts = " ".join(" ".join(c) for c in captured_cmds)
    assert "Chief Console.lnk" in cmd_texts
    assert "--console" in cmd_texts
    assert "Chief Installer.lnk" in cmd_texts
