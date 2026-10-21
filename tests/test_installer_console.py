"""Automated tests for Chief Console Desktop Application and Local Connectivity."""
import json
import os
from pathlib import Path
import ssl
import sys
import pytest

from deployment.installer.console import (
    ConsoleState,
    ConsoleDiagnostic,
    build_app_mode_command,
    find_desktop_runner,
    probe_local_console,
    console_smoke_test,
)
from deployment.installer.orchestrator import InstallerPaths


def test_probe_local_console_detects_incomplete_setup(tmp_path):
    paths = InstallerPaths(
        root=tmp_path / "root",
        tooling=tmp_path / "tooling",
        release_source=tmp_path / "release_source",
        runtime=tmp_path / "runtime",
        python=tmp_path / "python",
        node_root=tmp_path / "node",
        pwa_root=tmp_path / "pwa",
        managed=tmp_path / "managed",
        production=tmp_path / "production",
        caddy_root=tmp_path / "caddy",
        config_root=tmp_path / "config",
        config=tmp_path / "config" / "production-instance.json",
        identity_config=tmp_path / "config" / "installation-identity.json",
        browser_config=tmp_path / "config" / "browser.json",
        caddyfile=tmp_path / "config" / "Caddyfile",
        state_root=tmp_path / "state",
        meta=tmp_path / "meta",
        source_manifest=tmp_path / "manifest.json",
        backup=tmp_path / "backup",
        unit_stage=tmp_path / "unit_stage",
        unit_dest=tmp_path / "unit_dest",
        archive_base=tmp_path / "archive",
    )
    diag = probe_local_console(paths)
    assert diag.state == ConsoleState.SETUP_REQUIRED
    assert "not been completed" in diag.message.lower()
    assert "chief installer" in diag.remediation.lower()


def test_probe_local_console_detects_stopped_services(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    instance_config = config_dir / "production-instance.json"
    instance_config.write_text(
        json.dumps({
            "mode": "PRODUCTION",
            "public_host": "chief-farm.local",
            "dashboard_port": 18765,
            "tls_mode": "LOCAL",
        }),
        encoding="utf-8",
    )

    paths = InstallerPaths(
        root=tmp_path / "root",
        tooling=tmp_path / "tooling",
        release_source=tmp_path / "release_source",
        runtime=tmp_path / "runtime",
        python=tmp_path / "python",
        node_root=tmp_path / "node",
        pwa_root=tmp_path / "pwa",
        managed=tmp_path / "managed",
        production=tmp_path / "production",
        caddy_root=tmp_path / "caddy",
        config_root=config_dir,
        config=instance_config,
        identity_config=config_dir / "installation-identity.json",
        browser_config=config_dir / "browser.json",
        caddyfile=config_dir / "Caddyfile",
        state_root=tmp_path / "state",
        meta=tmp_path / "meta",
        source_manifest=tmp_path / "manifest.json",
        backup=tmp_path / "backup",
        unit_stage=tmp_path / "unit_stage",
        unit_dest=tmp_path / "unit_dest",
        archive_base=tmp_path / "archive",
    )

    # With ports 18765 and 443 closed
    diag = probe_local_console(paths)
    assert diag.state == ConsoleState.SERVICES_STOPPED
    assert diag.public_host == "chief-farm.local"
    assert diag.console_url == "https://chief-farm.local/"
    assert not diag.backend_active
    assert not diag.tls_proxy_active


def test_build_app_mode_command_preserves_desktop_isolation_and_loopback_rules(tmp_path):
    runner = "/usr/bin/chromium" if os.name != "nt" else r"C:\Program Files\Edge\msedge.exe"
    console_url = "https://chief-bigi.local/"
    public_host = "chief-bigi.local"
    profile_dir = tmp_path / "console_profile"

    cmd = build_app_mode_command(runner, console_url, public_host, profile_dir)

    # 1. Dedicated app window without browser URL bar / chrome
    assert cmd[0] == runner
    assert cmd[1] == "--app=https://chief-bigi.local/"

    # 2. Dedicated profile isolation
    assert f"--user-data-dir={profile_dir}" in cmd

    # 3. Direct loopback mapping independent of external DNS/mDNS
    assert "--host-resolver-rules=MAP chief-bigi.local 127.0.0.1" in cmd

    # 4. Native window manager class
    assert "--class=ChiefConsole" in cmd
    assert "--app-id=ChiefConsole" in cmd


def test_console_smoke_test_succeeds():
    assert console_smoke_test() == 0
