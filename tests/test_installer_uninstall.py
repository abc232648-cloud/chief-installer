"""Tests for graphical and headless uninstall flow, truthful reporting, and rollback preservation."""
import json
import os
from pathlib import Path
import pytest
import shutil
import tempfile

from test_installer_orchestrator import FakePlatformAdapter

from deployment.installer.errors import InstallerError
from deployment.installer.orchestrator import (
    InstallerOrchestrator,
    InstallerPaths,
    discover_configured_canonical_host,
    uninstall_chief,
)
from deployment.installer.privileged.linux import run_cleanup_local_discovery
from deployment.installer.ui.controller import InstallerUIController, ScreenId


class MockPlatformAdapter(FakePlatformAdapter):
    def __init__(self, target_os: str = "linux"):
        super().__init__(preflight_passes=True, has_existing_install=False)
        self.os_name = target_os
        self.stopped_services = []
        self.disabled_services = []
        self.unregistered_units = []
        self.cleaned_hosts = []

    def control_services(self, action: str, service_names):
        if action == "stop":
            self.stopped_services.extend(service_names)
        elif action == "disable":
            self.disabled_services.extend(service_names)
        return {s: True for s in service_names}

    def verify_service_active(self, service_name: str) -> bool:
        return False

    def terminate_service_processes(self, service_names):
        pass

    def unregister_service_units(self, unit_files):
        self.unregistered_units.extend(unit_files)

    def reload_service_manager(self):
        pass

    def remove_console_launchers(self, paths):
        return ["desktop-launcher"]

    def cleanup_local_discovery(self, public_host: str):
        self.cleaned_hosts.append(public_host)

    def open_url_in_browser(self, url: str) -> bool:
        return True


def test_unrelated_user_service_preserved_during_uninstall(tmp_path):
    """Linux systemd cleanup must strictly scope to Chief-owned unit names and never touch unrelated user services."""
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path)
    paths.unit_dest.mkdir(parents=True, exist_ok=True)

    # Chief unit files
    chief_units = [
        paths.unit_dest / "chief-agent-dashboard.service",
        paths.unit_dest / "chief-agent-worker.service",
        paths.unit_dest / "chief-agent-scheduler.service",
        paths.unit_dest / "chief-tls-proxy.service",
    ]
    for u in chief_units:
        u.write_text("[Unit]\nDescription=Chief\n", encoding="utf-8")

    # Unrelated user services in ~/.config/systemd/user
    unrelated_units = [
        paths.unit_dest / "syncthing.service",
        paths.unit_dest / "pipewire-custom.service",
        paths.unit_dest / "backup-nas.service",
    ]
    for u in unrelated_units:
        u.write_text("[Unit]\nDescription=User Custom Service\n", encoding="utf-8")

    adapter = MockPlatformAdapter(target_os="linux")
    report = uninstall_chief(paths=paths, platform=adapter, purge=False)

    assert report["success"] is True

    # Unrelated user service units MUST remain running, enabled, and untouched
    for u in unrelated_units:
        assert u.exists(), f"Unrelated unit {u.name} was erroneously deleted!"
        assert u.read_text(encoding="utf-8") == "[Unit]\nDescription=User Custom Service\n"

    # Only Chief unit files should be in unregistered_units
    unregistered_names = [u.name if isinstance(u, Path) else str(u) for u in adapter.unregistered_units]
    for u in unrelated_units:
        assert u.name not in unregistered_names
        assert f"{u.stem}.service" not in unregistered_names

    for u in chief_units:
        assert u.name in unregistered_names


def test_truthful_purge_failure_reported(tmp_path, monkeypatch):
    """Purge status must be truthful: never swallow deletion errors or report success when paths still exist."""
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path)
    paths.runtime.mkdir(parents=True, exist_ok=True)
    locked_file = paths.runtime / "locked.bin"
    locked_file.write_text("locked", encoding="utf-8")

    adapter = MockPlatformAdapter()

    # Simulate an undeletable directory (e.g. locked file or read-only volume)
    real_rmtree = shutil.rmtree

    def failing_rmtree(path, *args, **kwargs):
        if Path(path) == paths.runtime:
            raise PermissionError(f"[WinError 5] Access is denied: {path}")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", failing_rmtree)

    report = uninstall_chief(paths=paths, platform=adapter, purge=True)

    assert report["success"] is False, "Uninstall claimed success despite failed path deletion!"
    assert any("locked.bin" in str(item) or str(paths.runtime) in str(item) for item in report["failed_items"])
    assert str(paths.runtime) not in report["removed_paths"]


def test_normal_uninstall_preserves_database_and_credentials(tmp_path):
    """Standard uninstall must remove services, launchers, and runtime, but PRESERVE user data, database, and credentials."""
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path)

    # 1. User database & state
    paths.production.mkdir(parents=True, exist_ok=True)
    db_file = paths.production / "chief.sqlite"
    db_file.write_text("user farm data records", encoding="utf-8")

    # 2. Identity and instance secrets
    paths.identity_config.parent.mkdir(parents=True, exist_ok=True)
    paths.identity_config.write_text(json.dumps({"public_host": "farm.local", "owner": "Alice"}), encoding="utf-8")
    paths.config.write_text(json.dumps({"secret_token": "xyz123"}), encoding="utf-8")

    # 3. Caddy PKI / CA data
    ca_dir = paths.caddy_root / "data" / "caddy" / "pki" / "authorities" / "local"
    ca_dir.mkdir(parents=True, exist_ok=True)
    root_ca = ca_dir / "root.crt"
    root_ca.write_text("CERT DATA", encoding="utf-8")

    # 4. Exported setup folder
    device_setup = tmp_path / "Chief_Device_Setup"
    device_setup.mkdir(parents=True, exist_ok=True)
    wg_conf = device_setup / "Chief-Owner-WireGuard.conf"
    wg_conf.write_text("PrivateKey = secret", encoding="utf-8")

    # 5. Archive base
    paths.archive_base.mkdir(parents=True, exist_ok=True)
    archive_file = paths.archive_base / "backup-2026.tar.gz"
    archive_file.write_text("backup archive", encoding="utf-8")

    # 6. Runtime components (should be removed)
    paths.runtime.mkdir(parents=True, exist_ok=True)
    (paths.runtime / "python.exe").write_text("dummy", encoding="utf-8")
    paths.caddyfile.write_text("localhost { }", encoding="utf-8")
    paths.node_root.mkdir(parents=True, exist_ok=True)
    paths.tooling.mkdir(parents=True, exist_ok=True)
    paths.pwa_root.mkdir(parents=True, exist_ok=True)

    adapter = MockPlatformAdapter()
    report = uninstall_chief(paths=paths, platform=adapter, purge=False)

    assert report["success"] is True

    # VERIFY USER DATA IS SAFELY PRESERVED:
    assert db_file.exists(), "User database was erroneously deleted during standard uninstall!"
    assert db_file.read_text(encoding="utf-8") == "user farm data records"
    assert paths.identity_config.exists(), "Identity config was erroneously deleted!"
    assert paths.config.exists(), "Instance secrets config was erroneously deleted!"
    assert root_ca.exists(), "Local CA root certificate was erroneously deleted!"
    assert wg_conf.exists(), "Device setup WireGuard config was erroneously deleted!"
    assert archive_file.exists(), "Archive backup was erroneously deleted!"

    # VERIFY RUNTIME IS REMOVED:
    assert not paths.runtime.exists()
    assert not paths.caddyfile.exists()
    assert not paths.node_root.exists()
    assert not paths.tooling.exists()
    assert not paths.pwa_root.exists()


def test_full_purge_removes_database_and_credentials(tmp_path):
    """Full purge (purge=True) must remove everything including user database, credentials, and archives."""
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path)

    paths.production.mkdir(parents=True, exist_ok=True)
    (paths.production / "chief.sqlite").write_text("user farm data", encoding="utf-8")
    paths.identity_config.parent.mkdir(parents=True, exist_ok=True)
    paths.identity_config.write_text(json.dumps({"public_host": "farm.local"}), encoding="utf-8")
    paths.config.write_text(json.dumps({"token": "123"}), encoding="utf-8")
    paths.caddyfile.write_text("https://farm.local { }", encoding="utf-8")
    (paths.caddy_root / "data").mkdir(parents=True, exist_ok=True)
    paths.archive_base.mkdir(parents=True, exist_ok=True)
    paths.runtime.mkdir(parents=True, exist_ok=True)

    adapter = MockPlatformAdapter()
    report = uninstall_chief(paths=paths, platform=adapter, purge=True)

    assert report["success"] is True
    assert not paths.production.exists()
    assert not paths.identity_config.exists()
    assert not paths.config.exists()
    assert not paths.caddy_root.exists()
    assert not paths.archive_base.exists()
    assert not paths.runtime.exists()


def test_dynamic_canonical_host_discovery_and_cleanup(tmp_path):
    """Uninstall must discover dynamic canonical host without assuming 'chief.local'."""
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path)
    paths.identity_config.parent.mkdir(parents=True, exist_ok=True)
    paths.identity_config.write_text(
        json.dumps({"public_host": "chief-folio-9999.local", "owner": "Test"}),
        encoding="utf-8",
    )

    discovered = discover_configured_canonical_host(paths)
    assert discovered == "chief-folio-9999.local"

    adapter = MockPlatformAdapter()
    uninstall_chief(paths=paths, platform=adapter, purge=False)

    assert "chief-folio-9999.local" in adapter.cleaned_hosts


def test_cleanup_local_discovery_preserves_unrelated_hosts_entries(tmp_path, monkeypatch):
    """Privileged cleanup-local-discovery must remove ONLY the canonical host and preserve localhost and other mappings."""
    hosts_file = tmp_path / "hosts"
    initial_hosts = (
        "127.0.0.1\tlocalhost\n"
        "127.0.0.1\tchief-folio-1234.local\t# Chief on-farm console local host\n"
        "192.168.1.150\tmy-nas.local\n"
        "::1\tlocalhost ip6-localhost ip6-loopback\n"
    )
    hosts_file.write_text(initial_hosts, encoding="utf-8")

    # Patch /etc/hosts path in privileged/linux.py
    monkeypatch.setattr("deployment.installer.privileged.linux.Path", lambda p: hosts_file if p == "/etc/hosts" else Path(p))

    ret = run_cleanup_local_discovery("chief-folio-1234.local")
    assert ret == 0

    resulting_hosts = hosts_file.read_text(encoding="utf-8")
    assert "chief-folio-1234.local" not in resulting_hosts
    assert "127.0.0.1\tlocalhost" in resulting_hosts
    assert "192.168.1.150\tmy-nas.local" in resulting_hosts
    assert "::1\tlocalhost ip6-localhost ip6-loopback" in resulting_hosts


def test_failed_install_rollback_preserves_preexisting_user_data(tmp_path):
    """Failed install rollback must NEVER delete pre-existing user data."""
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path)

    # Create pre-existing user database and identity
    paths.production.mkdir(parents=True, exist_ok=True)
    db_file = paths.production / "chief.sqlite"
    db_file.write_text("PRE-EXISTING DATABASE RECORDS", encoding="utf-8")
    paths.identity_config.parent.mkdir(parents=True, exist_ok=True)
    paths.identity_config.write_text(json.dumps({"owner": "preexisting"}), encoding="utf-8")

    adapter = MockPlatformAdapter()
    from deployment.installer.models import InstallOptions
    from deployment.installer.manifest import CURRENT_RELEASE_MANIFEST

    options = InstallOptions(owner_name="New Attempt", owner_password="Pass")
    orchestrator = InstallerOrchestrator(
        platform=adapter,
        options=options,
        paths=paths,
        manifest=CURRENT_RELEASE_MANIFEST,
    )

    # Verify orchestrator detected pre-existing paths
    assert paths.production in orchestrator._preexisting_paths
    assert paths.identity_config in orchestrator._preexisting_paths

    # Simulate attempt creating temporary/partial files
    attempt_file = paths.runtime / "partial_attempt.tmp"
    attempt_file.parent.mkdir(parents=True, exist_ok=True)
    attempt_file.write_text("attempt", encoding="utf-8")
    orchestrator._track_created_file(attempt_file)

    # Attempt to track production directory as well
    orchestrator._track_created_file(paths.production)

    # Execute rollback
    orchestrator._rollback_attempt()

    # Attempt-created partial file must be gone
    assert not attempt_file.exists()

    # Pre-existing user database MUST BE PRESERVED:
    assert db_file.exists(), "Pre-existing database was wiped during install rollback!"
    assert db_file.read_text(encoding="utf-8") == "PRE-EXISTING DATABASE RECORDS"
    assert paths.identity_config.exists()


def test_gui_uninstall_controller_flow(tmp_path):
    """Graphical uninstaller controller must require confirmation before purge and support standard mode cleanly."""
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path)
    adapter = MockPlatformAdapter()
    controller = InstallerUIController(platform_adapter=adapter, paths=paths)

    # 1. Navigation to uninstallation screen
    controller.start_uninstall()
    assert controller.current_screen == ScreenId.UNINSTALL_OPTIONS
    assert controller.uninstall_mode == "standard"
    assert controller.purge_confirmed is False

    # 2. Refusal of purge mode without confirmation
    controller.set_uninstall_mode("purge")
    controller.set_purge_confirmed(False)
    with pytest.raises(ValueError) as exc_info:
        controller.execute_uninstall()
    assert "Confirmation is required" in str(exc_info.value)
    assert controller.current_screen == ScreenId.UNINSTALL_OPTIONS

    # 3. Successful execution with purge confirmed
    controller.set_purge_confirmed(True)
    import threading
    done_event = threading.Event()

    def callback(res):
        done_event.set()

    controller.execute_uninstall(callback=callback)
    done_event.wait(timeout=5.0)

    assert controller.current_screen == ScreenId.UNINSTALL_RESULT
    assert controller.uninstall_result is not None
    assert controller.uninstall_result.get("purge") is True
