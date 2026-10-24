import subprocess
"""Comprehensive verification tests for Chief uninstallation and cleanup.

Covers:
1. Linux integration test using an isolated systemd-user test harness exercising real LinuxPlatformAdapter.
2. Failed hostname, /etc/hosts, and Avahi cleanup affecting uninstall result truthfully.
3. Unrelated /etc/hosts entries (including legacy chief.local from other installations) surviving cleanup.
4. Explicit confirmation requirements on every purge entry point (GUI checkbox, CLI --confirm-purge, API).
5. Configurable temporary home directory: standard uninstall preserving DB/credentials/archives/unrelated files,
   and confirmed full removal deleting Chief data while preserving unrelated files.
6. GUI controller flow and packaged-GUI acceptance verifying accurate results and no terminal fallback on desktop.
"""
import io
import json
import os
from pathlib import Path
import pytest
import shutil
import sys
import threading

from test_installer_orchestrator import FakePlatformAdapter

from deployment.installer.cli import run_cli
from deployment.installer.errors import InstallerError, PrivilegeError
from deployment.installer.orchestrator import (
    InstallerOrchestrator,
    InstallerPaths,
    discover_configured_canonical_host,
    uninstall_chief,
)
from deployment.installer.platforms.linux import LinuxPlatformAdapter
from deployment.installer.privileged.linux import run_cleanup_local_discovery
from deployment.installer.ui.app import InstallerApp
from deployment.installer.ui.controller import InstallerUIController, ScreenId


class MockPlatformAdapter(FakePlatformAdapter):
    def __init__(self, target_os: str = "linux"):
        super().__init__(preflight_passes=True, has_existing_install=False)
        self.os_name = target_os
        self.stopped_services = []
        self.disabled_services = []
        self.unregistered_units = []
        self.cleaned_hosts = []
        self.cleanup_discovery_fails = False

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
        if self.cleanup_discovery_fails:
            raise PrivilegeError(f"Permission denied cleaning up local discovery for {public_host}")
        self.cleaned_hosts.append(public_host)
        return True

    def open_url_in_browser(self, url: str) -> bool:
        return True


def _create_systemd_harness(tmp_path, initial_active, initial_enabled):
    """Create an isolated, stateful systemctl test harness managing unit states in JSON."""
    state_file = tmp_path / "systemd_state.json"
    state = {
        "active": list(initial_active),
        "enabled": list(initial_enabled),
        "reloaded": False,
        "calls": [],
    }
    state_file.write_text(json.dumps(state), encoding="utf-8")

    script_code = f"""import sys, json
from pathlib import Path

state_file = Path(r"{state_file}")
try:
    state = json.loads(state_file.read_text(encoding="utf-8"))
except Exception:
    state = {{"active": [], "enabled": [], "calls": []}}

args = sys.argv[1:]
state.setdefault("calls", []).append(args)

if not args:
    sys.exit(0)

if args[0] == "--user":
    args = args[1:]

if not args:
    sys.exit(0)

cmd = args[0]
targets = args[1:]

if cmd == "is-active":
    unit = targets[-1] if targets else ""
    if unit in state.get("active", []):
        sys.exit(0)
    sys.exit(3)

elif cmd == "is-enabled":
    unit = targets[-1] if targets else ""
    if unit in state.get("enabled", []):
        sys.exit(0)
    sys.exit(1)

elif cmd == "stop":
    for u in targets:
        if u in state.get("active", []):
            state["active"].remove(u)
    state_file.write_text(json.dumps(state), encoding="utf-8")
    sys.exit(0)

elif cmd == "disable":
    for u in targets:
        if u in state.get("enabled", []):
            state["enabled"].remove(u)
    state_file.write_text(json.dumps(state), encoding="utf-8")
    sys.exit(0)

elif cmd in ("daemon-reload", "reset-failed"):
    state["reloaded"] = True
    state_file.write_text(json.dumps(state), encoding="utf-8")
    sys.exit(0)

sys.exit(0)
"""
    bin_dir = tmp_path / "systemd_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    runner_py = bin_dir / "systemctl_runner.py"
    runner_py.write_text(script_code, encoding="utf-8")

    if os.name == "nt":
        csc = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
        if csc.exists():
            py_path = sys.executable.replace("\\", "/")
            run_path = str(runner_py).replace("\\", "/")
            cs_source = (
                "using System;\n"
                "using System.Diagnostics;\n"
                "class Program {\n"
                "    static int Main(string[] args) {\n"
                "        var p = new Process();\n"
                f'        p.StartInfo.FileName = (char)34 + "{py_path}" + (char)34;\n'
                f'        p.StartInfo.Arguments = (char)34 + "{run_path}" + (char)34 + " " + string.Join(" ", args);\n'
                "        p.StartInfo.UseShellExecute = false;\n"
                "        p.Start();\n"
                "        p.WaitForExit();\n"
                "        return p.ExitCode;\n"
                "    }\n"
                "}\n"
            )
            cs_file = bin_dir / "systemctl.cs"
            exe_file = bin_dir / "systemctl.exe"
            cs_file.write_text(cs_source, encoding="utf-8")
            subprocess.run([str(csc), "/nologo", f"/out:{exe_file}", str(cs_file)], check=True)
        else:
            bat_file = bin_dir / "systemctl.cmd"
            bat_file.write_text(f'@"{sys.executable}" "{runner_py}" %*\n', encoding="utf-8")
    else:
        sh_file = bin_dir / "systemctl"
        sh_file.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{runner_py}" "$@"\n', encoding="utf-8")
        sh_file.chmod(0o755)

    return bin_dir, state_file


def _is_live_systemd_user_available() -> bool:
    """Check if a live systemd --user manager is running and reachable."""
    if os.name == "nt" or sys.platform != "linux":
        return False
    import shutil
    if not shutil.which("systemctl"):
        return False
    try:
        res = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True, timeout=2.0)
        return res.returncode == 0 or res.stdout.strip() in ("running", "degraded", "starting")
    except Exception:
        return False


def test_real_systemd_user_query_safe():
    """Safe test against a live systemd --user manager on a systemd-enabled runner.

    Queries status of a nonexistent unit to verify live systemctl interaction without
    modifying host services or changing files. Skips cleanly if live systemd is not present.
    """
    if not _is_live_systemd_user_available():
        pytest.skip("Live systemd --user manager not available in this environment")

    adapter = LinuxPlatformAdapter()
    assert adapter.verify_service_active("chief-nonexistent-sentinel-unit.service") is False


def test_linux_real_systemd_user_integration(tmp_path, monkeypatch):
    """Integration test invoking real LinuxPlatformAdapter with an isolated stateful fake

    systemctl harness (not a live systemd manager) to prove that uninstallation stops,
    disables, and deletes only Chief services while leaving unrelated services running,
    enabled, and byte-for-byte unchanged.
    """
    chief_services = [
        "chief-agent-dashboard.service",
        "chief-agent-worker.service",
        "chief-agent-scheduler.service",
        "chief-tls-proxy.service",
    ]
    unrelated_service = "unrelated-custom.service"

    initial_active = set(chief_services) | {unrelated_service}
    initial_enabled = set(chief_services) | {unrelated_service}

    bin_dir, state_file = _create_systemd_harness(tmp_path, initial_active, initial_enabled)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    paths.unit_dest.mkdir(parents=True, exist_ok=True)

    # Write byte-for-byte content for unrelated service
    unrelated_path = paths.unit_dest / unrelated_service
    unrelated_content = (
        "[Unit]\n"
        "Description=Farmer Custom Automatic Backup Service\n"
        "After=network.target\n\n"
        "[Service]\n"
        "ExecStart=/usr/local/bin/backup-farm-records.sh\n"
        "Restart=on-failure\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    unrelated_path.write_text(unrelated_content, encoding="utf-8")

    # Write Chief service files
    for s in chief_services:
        (paths.unit_dest / s).write_text(f"[Unit]\nDescription={s}\n", encoding="utf-8")

    # Use REAL LinuxPlatformAdapter (not a mock!)
    adapter = LinuxPlatformAdapter()
    monkeypatch.setattr(adapter, "cleanup_local_discovery", lambda host: True)

    # Verify initial active state in harness
    assert adapter.verify_service_active(unrelated_service) is True
    for s in chief_services:
        assert adapter.verify_service_active(s) is True

    # Execute Chief uninstallation
    report = uninstall_chief(paths=paths, platform=adapter, purge=False)
    assert report["success"] is True

    # Read final state from isolated systemd harness
    final_state = json.loads(state_file.read_text(encoding="utf-8"))

    # 1. UNRELATED SERVICE MUST REMAIN ACTIVE AND ENABLED:
    assert unrelated_service in final_state["active"], "Unrelated service was stopped by Chief uninstall!"
    assert unrelated_service in final_state["enabled"], "Unrelated service was disabled by Chief uninstall!"
    assert adapter.verify_service_active(unrelated_service) is True

    # 2. UNRELATED SERVICE FILE MUST BE BYTE-FOR-BYTE IDENTICAL:
    assert unrelated_path.exists(), "Unrelated service file was deleted!"
    assert unrelated_path.read_text(encoding="utf-8") == unrelated_content

    # 3. CHIEF SERVICES MUST BE STOPPED, DISABLED, AND REMOVED:
    for s in chief_services:
        assert s not in final_state["active"], f"Chief service {s} is still active!"
        assert s not in final_state["enabled"], f"Chief service {s} is still enabled!"
        assert not (paths.unit_dest / s).exists(), f"Chief service file {s} still exists on disk!"


def test_failed_discovery_cleanup_affects_uninstall_result(tmp_path):
    """Failed hostname, /etc/hosts, or Avahi cleanup must cause uninstall to report failure."""
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path)
    paths.identity_config.parent.mkdir(parents=True, exist_ok=True)
    paths.identity_config.write_text(
        json.dumps({"public_host": "chief-failed-host.local", "owner": "Farmer"}),
        encoding="utf-8",
    )

    adapter = MockPlatformAdapter()
    adapter.cleanup_discovery_fails = True  # Simulates permission error or /etc/hosts write failure

    report = uninstall_chief(paths=paths, platform=adapter, purge=False)

    assert report["success"] is False, "Uninstall claimed success despite failed discovery cleanup!"
    assert any("chief-failed-host.local" in item for item in report["failed_items"])


def test_cleanup_local_discovery_preserves_unrelated_hosts_and_legacy_chief_local(tmp_path, monkeypatch):
    """Local discovery cleanup removes ONLY the confirmed canonical host and preserves unrelated mappings."""
    hosts_file = tmp_path / "hosts"
    initial_hosts = (
        "127.0.0.1\tlocalhost\n"
        "127.0.0.1\tchief-my-farm.local\t# Chief on-farm console local host\n"
        "127.0.0.1\tchief.local\t# Unrelated or legacy service\n"
        "192.168.1.100\tnas-storage.local\n"
        "::1\tlocalhost ip6-localhost\n"
    )
    hosts_file.write_text(initial_hosts, encoding="utf-8")
    conf_file = tmp_path / "avahi-daemon.conf"
    conf_file.write_text("[server]\nhost-name=unrelated-farm\n", encoding="utf-8")
    service_file = tmp_path / "chief.service"
    service_file.write_text("pre-existing user service definition", encoding="utf-8")

    monkeypatch.setattr("deployment.installer.privileged.linux.Path", lambda p: hosts_file if p == "/etc/hosts" else Path(p))
    mock_run = lambda *args, **kwargs: subprocess.CompletedProcess(args=args[0] if args else [], returncode=0, stdout="", stderr="")
    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("deployment.installer.privileged.linux.subprocess.run", mock_run)

    # Clean up ONLY the confirmed canonical host for this installation
    ret = run_cleanup_local_discovery(
        "chief-my-farm.local",
        hosts_path_override=hosts_file,
        conf_path_override=conf_file,
        avahi_service_override=service_file,
    )
    assert ret == 0

    resulting_hosts = hosts_file.read_text(encoding="utf-8")

    # chief-my-farm.local must be removed
    assert "chief-my-farm.local" not in resulting_hosts

    # All unrelated hostnames (including chief.local from another installation) MUST SURVIVE:
    assert "127.0.0.1\tlocalhost" in resulting_hosts
    assert "chief.local" in resulting_hosts
    assert "192.168.1.100\tnas-storage.local" in resulting_hosts
    assert "::1\tlocalhost ip6-localhost" in resulting_hosts
    assert conf_file.read_text(encoding="utf-8") == "[server]\nhost-name=unrelated-farm\n"
    assert service_file.read_text(encoding="utf-8") == "pre-existing user service definition"


def test_cleanup_unknown_avahi_ownership_fails_without_modifying_config(tmp_path, monkeypatch):
    """A legacy matching Avahi hostname without an ownership record is preserved and reported."""
    from deployment.installer.privileged.linux import run_cleanup_local_discovery
    import shutil

    hosts = tmp_path / "hosts"
    hosts_original = "127.0.0.1\tlocalhost chief-farm.local\n"
    hosts.write_text(hosts_original, encoding="utf-8")
    conf = tmp_path / "avahi-daemon.conf"
    conf_original = "[server]\nhost-name=chief-farm\nuse-ipv4=yes\n"
    conf.write_text(conf_original, encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda _: None)

    result = run_cleanup_local_discovery(
        "chief-farm.local",
        hosts_path_override=hosts,
        conf_path_override=conf,
        state_path_override=tmp_path / "missing-state.json",
    )

    assert result == 1
    assert conf.read_text(encoding="utf-8") == conf_original
    assert hosts.read_text(encoding="utf-8") == hosts_original


def test_purge_entrypoints_require_explicit_confirmation(tmp_path, monkeypatch):
    """Every purge entry point (Python API, CLI non-interactive, CLI interactive, GUI) must require explicit confirmation."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))

    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path)
    adapter = MockPlatformAdapter()

    # 1. Direct Python API without confirmed=True must fail closed
    with pytest.raises(InstallerError) as exc_info:
        uninstall_chief(paths=paths, platform=adapter, purge=True, confirmed=False)
    assert "requires explicit confirmation" in str(exc_info.value)

    # 2. Direct Python API with confirmed=True succeeds
    report = uninstall_chief(paths=paths, platform=adapter, purge=True, confirmed=True)
    assert report["success"] is True

    # 3. CLI --purge with --non-interactive but without --confirm-purge must fail closed
    err_stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err_stream)
    code = run_cli(["--purge", "--non-interactive"], platform_adapter=adapter, paths=paths)
    assert code == 1
    assert "--confirm-purge" in err_stream.getvalue()

    # 4. CLI --purge with --confirm-purge succeeds
    code = run_cli(["--purge", "--confirm-purge"], platform_adapter=adapter, paths=paths)
    assert code == 0

    # 5. CLI interactive: typing anything other than 'CONFIRM' cancels
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "readline", lambda: "no\n")
    err_stream2 = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err_stream2)
    code = run_cli(["--purge"], platform_adapter=adapter, paths=paths)
    assert code == 1
    assert "Purge cancelled by user" in err_stream2.getvalue()

    # 6. CLI interactive: typing 'CONFIRM' succeeds
    monkeypatch.setattr(sys.stdin, "readline", lambda: "CONFIRM\n")
    code = run_cli(["--purge"], platform_adapter=adapter, paths=paths)
    assert code == 0

    # 7. GUI controller flow: purge unconfirmed raises ValueError
    controller = InstallerUIController(platform_adapter=adapter, paths=paths)
    controller.set_uninstall_mode("purge")
    controller.set_purge_confirmed(False)
    with pytest.raises(ValueError) as gui_exc:
        controller.execute_uninstall()
    assert "Confirmation is required" in str(gui_exc.value)


def test_standard_uninstall_vs_full_removal_with_temp_home(tmp_path, monkeypatch):
    """Test standard uninstall preserves DB, credentials, and unrelated files in a configurable temp home, while full purge deletes all."""
    custom_home = tmp_path / "custom_farmer_home"
    custom_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: custom_home)

    paths = InstallerPaths.create(custom_root=custom_home / ".local" / "share" / "chief", home=custom_home)

    def populate_environment():
        # User database
        paths.production.mkdir(parents=True, exist_ok=True)
        (paths.production / "chief.sqlite").write_text("ACTIVE DATABASE", encoding="utf-8")

        # Identity & Instance config
        paths.identity_config.parent.mkdir(parents=True, exist_ok=True)
        paths.identity_config.write_text(json.dumps({"public_host": "farm.local"}), encoding="utf-8")
        paths.config.write_text(json.dumps({"secret_token": "token_abc"}), encoding="utf-8")

        # Local CA cert
        ca_dir = paths.caddy_root / "data" / "caddy" / "pki" / "authorities" / "local"
        ca_dir.mkdir(parents=True, exist_ok=True)
        (ca_dir / "root.crt").write_text("CERT DATA", encoding="utf-8")

        # Exported device setup folder
        setup_dir = custom_home / "Chief_Device_Setup"
        setup_dir.mkdir(parents=True, exist_ok=True)
        (setup_dir / "Chief-Owner-WireGuard.conf").write_text("WG KEY", encoding="utf-8")

        # Historical archives
        paths.archive_base.mkdir(parents=True, exist_ok=True)
        (paths.archive_base / "archive-1.tar.gz").write_text("ARCHIVE", encoding="utf-8")

        # Unrelated user files in home directory
        docs_dir = custom_home / "Documents"
        docs_dir.mkdir(parents=True, exist_ok=True)
        unrelated_file1 = docs_dir / "crop_data.csv"
        unrelated_file1.write_text("corn,100,bushels", encoding="utf-8")

        unrelated_file2 = custom_home / "family_photo.png"
        unrelated_file2.write_text("IMAGE_BYTES", encoding="utf-8")

        # Runtime files (should be removed)
        paths.runtime.mkdir(parents=True, exist_ok=True)
        (paths.runtime / "python.bin").write_text("bin", encoding="utf-8")
        paths.caddyfile.write_text("localhost { }", encoding="utf-8")

        return unrelated_file1, unrelated_file2

    adapter = MockPlatformAdapter()

    # --- Phase 1: Standard Uninstall ---
    unrelated1, unrelated2 = populate_environment()
    rep_std = uninstall_chief(paths=paths, platform=adapter, purge=False)
    assert rep_std["success"] is True

    # Preserved user data:
    assert (paths.production / "chief.sqlite").exists()
    assert paths.identity_config.exists()
    assert paths.config.exists()
    assert (paths.caddy_root / "data" / "caddy" / "pki" / "authorities" / "local" / "root.crt").exists()
    assert (custom_home / "Chief_Device_Setup" / "Chief-Owner-WireGuard.conf").exists()
    assert (paths.archive_base / "archive-1.tar.gz").exists()

    # Preserved unrelated files:
    assert unrelated1.exists()
    assert unrelated1.read_text(encoding="utf-8") == "corn,100,bushels"
    assert unrelated2.exists()

    # Cleaned runtime:
    assert not paths.runtime.exists()
    assert not paths.caddyfile.exists()

    # --- Phase 2: Confirmed Full Removal (Purge) ---
    unrelated1, unrelated2 = populate_environment()
    rep_purge = uninstall_chief(paths=paths, platform=adapter, purge=True, confirmed=True)
    assert rep_purge["success"] is True

    # All Chief data must be permanently removed:
    assert not paths.production.exists()
    assert not paths.identity_config.exists()
    assert not paths.config.exists()
    assert not paths.caddy_root.exists()
    assert not (custom_home / "Chief_Device_Setup").exists()
    assert not paths.archive_base.exists()
    assert not paths.runtime.exists()

    # Unrelated files must still be preserved:
    assert unrelated1.exists()
    assert unrelated1.read_text(encoding="utf-8") == "corn,100,bushels"
    assert unrelated2.exists()


def test_packaged_gui_uninstall_modes_and_terminal_fallback_protection(tmp_path, monkeypatch):
    """Packaged-GUI acceptance: opens uninstall screen, handles modes, rejects unconfirmed purge, and protects against terminal fallback."""
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path)
    adapter = MockPlatformAdapter()

    has_display = True
    if sys.platform != "win32" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        has_display = False

    if has_display:
        # 1. GUI App construction and screen rendering
        app = InstallerApp(platform_adapter=adapter)
        app.controller.paths = paths

        try:
            # Start at uninstall screen
            app.controller.start_uninstall()
            app._render_screen(app.controller.current_screen)
            app.update()
            assert app.controller.current_screen == ScreenId.UNINSTALL_OPTIONS

            # 2. Mode toggling & unconfirmed purge rejection
            app._on_uninstall_mode_changed("purge", confirmed=False)
            app.update()
            assert str(app.uninstall_submit_btn.cget("state")) == "disabled"

            # Toggle checkbox
            app._on_uninstall_confirm_toggled("purge", confirmed=True)
            app.update()
            assert str(app.uninstall_submit_btn.cget("state")) in ("normal", "active")

            # 3. Execution of full purge
            t1 = app.controller.execute_uninstall()
            t1.join(timeout=5.0)
            app._render_screen(app.controller.current_screen)
            app.update()
            assert app.controller.current_screen == ScreenId.UNINSTALL_RESULT
            assert app.controller.uninstall_result["success"] is True
            assert app.controller.uninstall_result["purge"] is True

            # 4. Failure scenario
            adapter.cleanup_discovery_fails = True
            paths.identity_config.parent.mkdir(parents=True, exist_ok=True)
            paths.identity_config.write_text(json.dumps({"public_host": "chief-fail.local"}), encoding="utf-8")

            app.controller.start_uninstall()
            app._render_screen(app.controller.current_screen)
            app.update()
            t2 = app.controller.execute_uninstall()
            t2.join(timeout=5.0)
            app._render_screen(app.controller.current_screen)
            app.update()
            assert app.controller.current_screen == ScreenId.UNINSTALL_RESULT
            assert app.controller.uninstall_result["success"] is False
            assert "chief-fail.local" in app.controller.uninstall_error
        finally:
            app.destroy()
    else:
        # Headless controller flow verification
        controller = InstallerUIController(platform_adapter=adapter, paths=paths)
        controller.start_uninstall()
        assert controller.current_screen == ScreenId.UNINSTALL_OPTIONS

        # Mode selection and purge confirmation rejection
        controller.set_uninstall_mode("purge")
        controller.set_purge_confirmed(False)
        with pytest.raises(ValueError):
            controller.execute_uninstall()

        # Confirmed purge execution
        controller.set_purge_confirmed(True)
        t1 = controller.execute_uninstall()
        t1.join(timeout=5.0)
        assert controller.current_screen == ScreenId.UNINSTALL_RESULT
        assert controller.uninstall_result["success"] is True
        assert controller.uninstall_result["purge"] is True

        # Failure scenario
        adapter.cleanup_discovery_fails = True
        paths.identity_config.parent.mkdir(parents=True, exist_ok=True)
        paths.identity_config.write_text(json.dumps({"public_host": "chief-fail.local"}), encoding="utf-8")
        controller.start_uninstall()
        t2 = controller.execute_uninstall()
        t2.join(timeout=5.0)
        assert controller.current_screen == ScreenId.UNINSTALL_RESULT
        assert controller.uninstall_result["success"] is False
        assert "chief-fail.local" in controller.uninstall_error

    # 5. Verify that GUI launch failures NEVER fall back to interactive terminal on desktop
    from deployment.installer.__main__ import main as installer_main

    # Case A: Display present but launch fails -> must exit 1 without terminal fallback
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys, "argv", ["chief-installer", "--gui"])

    def failing_launch_gui(*args, **kwargs):
        raise RuntimeError("Display connection refused")

    monkeypatch.setattr("deployment.installer.ui.app.launch_gui", failing_launch_gui)

    err_buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err_buf)

    exit_code = installer_main()
    assert exit_code == 1, "GUI failure must exit with non-zero code!"
    assert "GUI launch failed" in err_buf.getvalue()
    assert "Chief Installation" not in err_buf.getvalue()

    # Case B: No display present with non-tty stdin -> must exit 1 without terminal fallback
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    err_buf2 = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err_buf2)
    monkeypatch.setattr(sys, "argv", ["chief-installer", "--gui"])

    exit_code2 = installer_main()
    assert exit_code2 == 1
    assert ("Graphical interface requested" in err_buf2.getvalue() or "GUI launch failed" in err_buf2.getvalue())
    assert "Chief Installation" not in err_buf2.getvalue()


def test_purge_preserves_default_home_sentinel_when_custom_home_supplied(tmp_path, monkeypatch):
    """Keep the default-profile sentinel intact while purging a different temp home.

    Both profiles are pytest temporary directories: this test verifies the real
    Path.home() decision path without writing into the developer's account.
    """
    default_home = tmp_path / "default_profile"
    monkeypatch.setattr(Path, "home", lambda: default_home)
    real_setup_dir = default_home / "Chief_Device_Setup"
    real_setup_dir.mkdir(parents=True, exist_ok=True)
    real_sentinel = real_setup_dir / "sentinel.txt"
    try:
        real_sentinel.write_text("ACTIVE_FARM_REAL_SENTINEL", encoding="utf-8")
        temp_home = tmp_path / "temp_farmer_home"
        temp_root = tmp_path / "temp_chief_root"
        temp_paths = InstallerPaths.create(custom_root=temp_root, home=temp_home)

        # Create temporary device setup files and database in temp paths
        temp_paths.device_setup_dir.mkdir(parents=True, exist_ok=True)
        temp_wg_file = temp_paths.device_setup_dir / "Chief-Owner-WireGuard.conf"
        temp_wg_file.write_text("TEMP_WIREGUARD_CONFIG", encoding="utf-8")

        temp_paths.production.mkdir(parents=True, exist_ok=True)
        (temp_paths.production / "chief.sqlite").write_text("TEMP_DATABASE", encoding="utf-8")

        temp_paths.config_root.mkdir(parents=True, exist_ok=True)
        (temp_paths.config_root / "production-instance.json").write_text("{}", encoding="utf-8")

        adapter = MockPlatformAdapter()

        # Run full confirmed purge targeting temp_paths
        report = uninstall_chief(paths=temp_paths, platform=adapter, purge=True, confirmed=True)
        assert report["success"] is True

        # 1. Temporary setup files must be deleted
        assert not temp_wg_file.exists()
        assert not temp_paths.device_setup_dir.exists()
        assert not temp_paths.production.exists()

        # 2. Real home sentinel MUST be completely untouched!
        assert real_sentinel.exists()
        assert real_sentinel.read_text(encoding="utf-8") == "ACTIVE_FARM_REAL_SENTINEL"
    finally:
        if real_sentinel.exists():
            real_sentinel.unlink()
        if real_setup_dir.exists() and not any(real_setup_dir.iterdir()):
            real_setup_dir.rmdir()


def test_purge_safety_guard_rejects_targets_outside_temp_home(tmp_path, monkeypatch):
    """Verify that if a purge target resolves outside supplied boundaries or to real home,

    uninstall_chief fails closed immediately before touching any files.
    """
    temp_home = tmp_path / "temp_home"
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "default_profile")
    temp_root = tmp_path / "temp_root"
    temp_paths = InstallerPaths.create(custom_root=temp_root, home=temp_home)

    adapter = MockPlatformAdapter()

    # Case A: An external target outside temp boundaries
    rogue_paths = InstallerPaths.create(custom_root=temp_root, home=temp_home)
    rogue_paths.device_setup_dir = Path("/etc/unrelated_system_path")

    with pytest.raises(InstallerError) as exc_info:
        uninstall_chief(paths=rogue_paths, platform=adapter, purge=True, confirmed=True)
    assert "Safety guard violation" in str(exc_info.value)
    assert adapter.stopped_services == []
    assert adapter.disabled_services == []
    assert adapter.unregistered_units == []

    # Case B: When custom temp home is supplied, target pointing to real home setup dir must fail
    rogue_paths_real = InstallerPaths.create(custom_root=temp_root, home=temp_home)
    rogue_paths_real.device_setup_dir = (tmp_path / "default_profile" / "Chief_Device_Setup")

    with pytest.raises(InstallerError) as exc_info2:
        uninstall_chief(paths=rogue_paths_real, platform=adapter, purge=True, confirmed=True)
    assert "Safety guard violation" in str(exc_info2.value)
    assert adapter.stopped_services == []
    assert adapter.disabled_services == []
    assert adapter.unregistered_units == []


def test_avahi_and_hosts_cleanup_preserves_unrelated_settings_and_services(tmp_path, monkeypatch):
    """Verify Avahi and hosts cleanup only removes the installation's configured hostname

    while preserving unrelated hosts aliases, Avahi settings, other service files, and propagates restart errors.
    """
    from deployment.installer.privileged.linux import run_cleanup_local_discovery

    # 1. Setup isolated /etc/hosts with Chief and unrelated entries
    hosts_path = tmp_path / "hosts"
    hosts_path.write_text(
        "127.0.0.1\tlocalhost unrelated-host.local\n"
        "::1\tlocalhost ip6-localhost other-alias.local\n"
        "192.168.1.50\tnas.farm.local\n",
        encoding="utf-8",
    )

    # 2. Setup isolated Avahi config with a previous hostname and unrelated settings.
    conf_path = tmp_path / "avahi-daemon.conf"
    initial_conf = (
        "# Avahi daemon configuration\n"
        "[server]\n"
        "host-name=previous-farm\n"
        "use-ipv4=yes\n"
        "ratelimit-interval-usec=1000000\n"
        "\n"
        "[publish]\n"
        "publish-addresses=yes\n"
        "publish-hinfo=no\n"
    )
    conf_path.write_text(initial_conf, encoding="utf-8")
    state_path = conf_path.with_name(conf_path.name + ".chief-state")

    # The installer does not create or track these service files, so cleanup
    # must preserve both.
    services_dir = tmp_path / "services"
    services_dir.mkdir(parents=True, exist_ok=True)
    chief_service = services_dir / "chief.service"
    chief_service.write_text("<service-group><name>Chief</name></service-group>", encoding="utf-8")
    unrelated_service = services_dir / "ssh.service"
    unrelated_service.write_text("<service-group><name>SSH</name></service-group>", encoding="utf-8")

    # Mock systemctl operations succeeding.
    import shutil
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/bin/{cmd}")
    mock_run = lambda *args, **kwargs: subprocess.CompletedProcess(args=args[0] if args else [], returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", mock_run)

    # Exercise setup and uninstall as a pair. Setup records the existing
    # hostname before replacing it; cleanup must restore it exactly.
    from deployment.installer.privileged.linux import run_configure_avahi
    setup_rc = run_configure_avahi(
        "my-farm",
        conf_path_override=conf_path,
        hosts_path_override=hosts_path,
        nsswitch_path_override=tmp_path / "nsswitch.conf",
        state_path_override=state_path,
    )
    assert setup_rc == 0
    assert "host-name=my-farm\n" in conf_path.read_text(encoding="utf-8")
    assert state_path.exists()

    rc = run_cleanup_local_discovery(
        "my-farm.local",
        hosts_path_override=hosts_path,
        conf_path_override=conf_path,
        avahi_service_override=chief_service,
        state_path_override=state_path,
    )
    assert rc == 0

    # Verify hosts cleanup: only my-farm.local removed from loopback lines
    hosts_lines = hosts_path.read_text(encoding="utf-8").splitlines()
    assert "127.0.0.1\tlocalhost unrelated-host.local" in hosts_lines
    assert "::1\tlocalhost ip6-localhost other-alias.local" in hosts_lines
    assert "192.168.1.50\tnas.farm.local" in hosts_lines
    assert "my-farm.local" not in hosts_path.read_text(encoding="utf-8")

    # Verify Avahi cleanup restores the previous hostname and all other settings.
    conf_result = conf_path.read_text(encoding="utf-8")
    assert conf_result == initial_conf
    assert not state_path.exists()
    assert "[server]" in conf_result
    assert "use-ipv4=yes" in conf_result
    assert "ratelimit-interval-usec=1000000" in conf_result
    assert "[publish]" in conf_result
    assert "publish-addresses=yes" in conf_result
    assert "publish-hinfo=no" in conf_result

    # Neither service file is known to be installer-owned, so preserve both.
    assert chief_service.exists()
    assert "Chief" in chief_service.read_text(encoding="utf-8")
    assert unrelated_service.exists()
    assert "SSH" in unrelated_service.read_text(encoding="utf-8")

    # Verify restart error propagation: if systemctl try-restart fails, returncode 1 is returned
    mock_fail_run = lambda *args, **kwargs: subprocess.CompletedProcess(args=args[0] if args else [], returncode=1, stdout="", stderr="failed to restart")
    monkeypatch.setattr(subprocess, "run", mock_fail_run)
    err_rc = run_cleanup_local_discovery(
        "my-farm.local",
        hosts_path_override=hosts_path,
        conf_path_override=conf_path,
        avahi_service_override=chief_service,
        state_path_override=state_path,
    )
    assert err_rc == 1
