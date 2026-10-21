"""Tests for Windows platform adapter, Task Scheduler integration, and Windows installer orchestration."""
import json
import os
from pathlib import Path
import platform as py_platform
import xml.etree.ElementTree as ET
import pytest

from deployment.installer.manifest import ACCEPTED_FIRST_RELEASE_MANIFEST
from deployment.installer.models import InstallOptions, StepId, StepStatus
from deployment.installer.orchestrator import InstallerOrchestrator, InstallerPaths
from deployment.installer.platforms.windows import WindowsPlatformAdapter


def test_windows_platform_info():
    adapter = WindowsPlatformAdapter()
    info = adapter.get_platform_info()
    assert info.os_name == "windows"
    assert info.distro_name == "Windows"
    if py_platform.system() == "Windows":
        assert info.architecture == "x86_64"
        assert info.is_supported is True


def test_windows_username():
    adapter = WindowsPlatformAdapter()
    username = adapter.get_current_username()
    assert isinstance(username, str)
    assert len(username) > 0


def test_windows_preflight_checks():
    adapter = WindowsPlatformAdapter()
    report = adapter.run_preflight_checks()
    assert report.platform.os_name == "windows"
    check_names = {c.name for c in report.checks}
    assert "os_windows" in check_names
    assert "windows_version_supported" in check_names
    assert "cpu_architecture" in check_names
    assert "disk_space" in check_names
    assert "task_scheduler" in check_names
    if py_platform.system() == "Windows":
        assert report.passed is True


def test_windows_normalize_task_name():
    adapter = WindowsPlatformAdapter()
    assert adapter._normalize_task_name("chief-agent-dashboard.service") == "chief-agent-dashboard"
    assert adapter._normalize_task_name("chief-agent-worker.xml") == "chief-agent-worker"
    assert adapter._normalize_task_name("chief-tls-proxy") == "chief-tls-proxy"


def test_windows_local_discovery():
    adapter = WindowsPlatformAdapter()
    host = adapter.configure_local_discovery("Green Valley Farm")
    assert host.startswith("chief-green-valley-farm")
    assert host.endswith(".local")


def test_windows_paths_layout_custom_root(tmp_path):
    paths = InstallerPaths.create(custom_root=tmp_path / "chief_root", home=tmp_path / "home")
    assert paths.root == tmp_path / "chief_root"
    assert paths.python == tmp_path / "chief_root" / "runtime" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")


def test_windows_paths_default_appdata(monkeypatch, tmp_path):
    local_dir = tmp_path / "AppData" / "Local"
    roaming_dir = tmp_path / "AppData" / "Roaming"
    monkeypatch.setenv("LOCALAPPDATA", str(local_dir))
    monkeypatch.setenv("APPDATA", str(roaming_dir))

    if os.name == "nt":
        paths = InstallerPaths.create(home=tmp_path)
        assert paths.root == local_dir / "chief"
        assert paths.config_root == roaming_dir / "chief"
        assert paths.unit_dest == local_dir / "chief" / "tasks"
        assert paths.python == local_dir / "chief" / "runtime" / "Scripts" / "python.exe"


def test_windows_caddyfile_spaces_and_slashes(tmp_path):
    root_with_spaces = tmp_path / "Program Data" / "Chief User"
    paths = InstallerPaths.create(custom_root=root_with_spaces, home=tmp_path)
    options = InstallOptions(
        owner_name="Test Owner",
        owner_username="admin",
        owner_password="Password123!",
        tls_mode="LOCAL",
    )
    orchestrator = InstallerOrchestrator(
        platform=WindowsPlatformAdapter(),
        options=options,
        manifest=ACCEPTED_FIRST_RELEASE_MANIFEST,
        paths=paths,
    )
    caddyfile = orchestrator.generate_caddyfile("chief-test.local", "LOCAL")
    assert "tls internal" in caddyfile
    assert "    handle {\n        reverse_proxy 127.0.0.1:8765" in caddyfile
    assert "respond 404" not in caddyfile
    assert "header_up Host {host}" in caddyfile
    assert "header_up X-Forwarded-Proto https" in caddyfile
    for client in ("owner", "staff", "jobs"):
        assert f"handle_path /{client}/*" in caddyfile
    # Forward slashes must be present, no backslashes in root directives
    assert "\\" not in caddyfile
    assert f'root * "{str(paths.pwa_root).replace(chr(92), "/")}/owner/dist"' in caddyfile


def test_windows_caddy_task_xml_generation(tmp_path):
    paths = InstallerPaths.create(custom_root=tmp_path / "chief", home=tmp_path)
    options = InstallOptions(
        owner_name="Test Owner",
        owner_username="admin",
        owner_password="Password123!",
        tls_mode="LOCAL",
    )
    orchestrator = InstallerOrchestrator(
        platform=WindowsPlatformAdapter(),
        options=options,
        manifest=ACCEPTED_FIRST_RELEASE_MANIFEST,
        paths=paths,
    )
    xml_str = orchestrator.generate_caddy_task_xml("TestUser")
    # Must be valid XML
    root = ET.fromstring(xml_str)
    assert root.tag.endswith("Task")
    # Must include LogonTrigger
    assert root.find(".//{http://schemas.microsoft.com/windows/2004/02/mit/task}LogonTrigger") is not None
    # Must include Principals with InteractiveToken and LeastPrivilege
    principal = root.find(".//{http://schemas.microsoft.com/windows/2004/02/mit/task}Principal")
    assert principal is not None
    assert principal.find("{http://schemas.microsoft.com/windows/2004/02/mit/task}LogonType").text == "InteractiveToken"
    assert principal.find("{http://schemas.microsoft.com/windows/2004/02/mit/task}RunLevel").text == "LeastPrivilege"
    # Action exec must point to caddy
    exec_node = root.find(".//{http://schemas.microsoft.com/windows/2004/02/mit/task}Exec")
    assert exec_node is not None
    assert "caddy" in exec_node.find("{http://schemas.microsoft.com/windows/2004/02/mit/task}Command").text.lower()


def test_windows_export_device_ca(tmp_path):
    adapter = WindowsPlatformAdapter()
    ca_cert = tmp_path / "root.crt"
    ca_cert.write_text("-----BEGIN CERTIFICATE-----\nWINDOWS-CA\n-----END CERTIFICATE-----\n", encoding="utf-8")
    dest_dir = tmp_path / "Device_Setup"

    readme = adapter.export_device_ca(ca_cert, dest_dir, "Windows Owner", "chief-windows.local")
    assert readme.exists()
    assert (dest_dir / "Chief-Local-CA.crt").exists()
    assert (dest_dir / "Chief-Local-CA.crt").read_text(encoding="utf-8") == ca_cert.read_text(encoding="utf-8")
    readme_text = readme.read_text(encoding="utf-8")
    assert "chief-windows.local" in readme_text
    assert "https://chief-windows.local/" in readme_text
    assert "https://chief-windows.local/owner/" in readme_text
    assert "Windows Owner" in readme_text


def test_windows_archive_existing_installation(tmp_path, monkeypatch):
    local_app = tmp_path / "AppData" / "Local"
    roaming_app = tmp_path / "AppData" / "Roaming"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app))
    monkeypatch.setenv("APPDATA", str(roaming_app))

    chief_local = local_app / "chief"
    chief_local.mkdir(parents=True)
    (chief_local / "production").mkdir()
    (chief_local / "production" / "chief.sqlite").write_text("data", encoding="utf-8")

    adapter = WindowsPlatformAdapter()
    has_existing, candidates = adapter.detect_existing_installation()
    assert has_existing is True

    archive_base = tmp_path / "Chief_Previous_Installations"
    target = adapter.archive_existing_installation(archive_base)
    assert target.exists()
    assert (target / "ARCHIVE_SUMMARY.txt").exists()
    # Original should be moved
    assert not (chief_local / "production").exists()


def test_windows_caddy_storage_and_service_cmd(tmp_path):
    """Verify explicit per-install Caddy storage location, service cmd script, and task XML."""
    paths = InstallerPaths.create(custom_root=tmp_path / "chief", home=tmp_path)
    options = InstallOptions(
        owner_name="Caddy Storage Test",
        owner_username="admin",
        owner_password="Password123!",
        tls_mode="LOCAL",
    )
    orchestrator = InstallerOrchestrator(
        platform=WindowsPlatformAdapter(),
        options=options,
        manifest=ACCEPTED_FIRST_RELEASE_MANIFEST,
        paths=paths,
    )
    caddyfile = orchestrator.generate_caddyfile("chief-test.local", "LOCAL")
    expected_storage = str(paths.caddy_root / "data" / "caddy").replace("\\", "/")
    assert f'storage file_system "{expected_storage}"' in caddyfile

    # Service cmd script sets explicit environment variables
    cmd_script = orchestrator.generate_caddy_service_cmd()
    assert "setlocal" in cmd_script
    data_dir = str(paths.caddy_root / "data" / "caddy")
    data_home = str(paths.caddy_root / "data")
    config_dir = str(paths.caddy_root / "config" / "caddy")
    config_home = str(paths.caddy_root / "config")
    assert f'set "CADDY_DATA_DIR={data_dir}"' in cmd_script
    assert f'set "XDG_DATA_HOME={data_home}"' in cmd_script
    assert f'set "CADDY_CONFIG_DIR={config_dir}"' in cmd_script
    assert f'set "XDG_CONFIG_HOME={config_home}"' in cmd_script
    assert "run --config" in cmd_script

    # Task XML executes caddy_service.cmd
    xml_str = orchestrator.generate_caddy_task_xml("TestUser")
    root = ET.fromstring(xml_str)
    exec_node = root.find(".//{http://schemas.microsoft.com/windows/2004/02/mit/task}Exec")
    assert exec_node is not None
    cmd_text = exec_node.find("{http://schemas.microsoft.com/windows/2004/02/mit/task}Command").text
    assert "caddy_service.cmd" in cmd_text
    assert "caddy" in cmd_text.lower()


def test_windows_task_status_parsing(monkeypatch):
    """Verify get_task_status correctly parses schtasks CSV output or returns None."""
    adapter = WindowsPlatformAdapter()

    class MockProc:
        def __init__(self, code, out="", err=""):
            self.returncode = code
            self.stdout = out
            self.stderr = err

    # Running
    monkeypatch.setattr(
        "deployment.installer.platforms.windows.run_quiet",
        lambda *args, **kwargs: MockProc(0, '"\\chief-agent-dashboard","N/A","Running"\n')
    )
    assert adapter.get_task_status("chief-agent-dashboard") == "Running"

    # Ready
    monkeypatch.setattr(
        "deployment.installer.platforms.windows.run_quiet",
        lambda *args, **kwargs: MockProc(0, '"\\chief-agent-dashboard","N/A","Ready"\n')
    )
    assert adapter.get_task_status("chief-agent-dashboard") == "Ready"

    # Disabled
    monkeypatch.setattr(
        "deployment.installer.platforms.windows.run_quiet",
        lambda *args, **kwargs: MockProc(0, '"\\chief-agent-dashboard","N/A","Disabled"\n')
    )
    assert adapter.get_task_status("chief-agent-dashboard") == "Disabled"

    # Missing task
    monkeypatch.setattr(
        "deployment.installer.platforms.windows.run_quiet",
        lambda *args, **kwargs: MockProc(1, "", "ERROR: The system cannot find the file specified.")
    )
    assert adapter.get_task_status("nonexistent-task") is None


def test_windows_service_readiness_states(monkeypatch):
    """Verify verify_service_active distinguishes Running from Ready, Disabled, and missing."""
    adapter = WindowsPlatformAdapter()

    class MockProc:
        def __init__(self, code, out="", err=""):
            self.returncode = code
            self.stdout = out
            self.stderr = err

    # 1. Ready is NOT active
    monkeypatch.setattr(
        "deployment.installer.platforms.windows.run_quiet",
        lambda *args, **kwargs: MockProc(0, '"\\chief-agent-worker","N/A","Ready"\n')
    )
    assert adapter.verify_service_active("chief-agent-worker") is False

    # 2. Disabled is NOT active
    monkeypatch.setattr(
        "deployment.installer.platforms.windows.run_quiet",
        lambda *args, **kwargs: MockProc(0, '"\\chief-agent-worker","N/A","Disabled"\n')
    )
    assert adapter.verify_service_active("chief-agent-worker") is False

    # 3. Missing task is NOT active
    monkeypatch.setattr(
        "deployment.installer.platforms.windows.run_quiet",
        lambda *args, **kwargs: MockProc(1, "", "ERROR: Not found")
    )
    assert adapter.verify_service_active("chief-agent-worker") is False

    # 4. Running for non-listener task (worker) IS active
    monkeypatch.setattr(
        "deployment.installer.platforms.windows.run_quiet",
        lambda *args, **kwargs: MockProc(0, '"\\chief-agent-worker","N/A","Running"\n')
    )
    assert adapter.verify_service_active("chief-agent-worker") is True

    # 5. Running for dashboard listener requires port 8765
    monkeypatch.setattr(
        "deployment.installer.platforms.windows.run_quiet",
        lambda *args, **kwargs: MockProc(0, '"\\chief-agent-dashboard","N/A","Running"\n')
    )
    # When port is listening
    monkeypatch.setattr(adapter, "_is_port_listening", lambda port, **kw: port == 8765)
    assert adapter.verify_service_active("chief-agent-dashboard") is True

    # When port is not listening
    monkeypatch.setattr(adapter, "_is_port_listening", lambda port, **kw: False)
    assert adapter.verify_service_active("chief-agent-dashboard") is False


def test_windows_task_unregistration(monkeypatch):
    """Verify unregister_service_units invokes schtasks delete and removes files."""
    adapter = WindowsPlatformAdapter()
    calls = []

    class MockProc:
        def __init__(self, code):
            self.returncode = code
            self.stdout = ""
            self.stderr = ""

    def mock_run(cmd, **kwargs):
        calls.append(cmd)
        return MockProc(0)

    monkeypatch.setattr("deployment.installer.platforms.windows.run_quiet", mock_run)

    adapter.unregister_service_units([
        Path("chief-agent-dashboard.xml"),
        Path("chief-tls-proxy.xml"),
    ])

    cmd_strs = [" ".join(c) for c in calls]
    assert any("schtasks /end /tn chief-agent-dashboard" in s for s in cmd_strs)
    assert any("schtasks /delete /tn chief-agent-dashboard /f" in s for s in cmd_strs)
    assert any("schtasks /end /tn chief-tls-proxy" in s for s in cmd_strs)
    assert any("schtasks /delete /tn chief-tls-proxy /f" in s for s in cmd_strs)


def test_windows_launcher_cleanup(tmp_path, monkeypatch):
    """Verify remove_console_launchers removes shortcuts and empty directory."""
    desktop = tmp_path / "Desktop"
    desktop.mkdir(parents=True)
    start_menu = tmp_path / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Chief"
    start_menu.mkdir(parents=True)

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))

    # Create dummy shortcuts
    (desktop / "Chief Console.lnk").write_text("dummy", encoding="utf-8")
    (start_menu / "Chief Console.lnk").write_text("dummy", encoding="utf-8")
    (start_menu / "Chief Installer.lnk").write_text("dummy", encoding="utf-8")

    adapter = WindowsPlatformAdapter()
    removed = adapter.remove_console_launchers(None)

    assert not (desktop / "Chief Console.lnk").exists()
    assert not (start_menu / "Chief Console.lnk").exists()
    assert not (start_menu / "Chief Installer.lnk").exists()
    assert not start_menu.exists()  # Empty folder should be removed
    assert len(removed) >= 3


def test_windows_uninstall_and_full_purge(tmp_path, monkeypatch):
    """Verify uninstall_chief stops services, removes credentials, and leaves unrelated files untouched."""
    local_app = tmp_path / "AppData" / "Local"
    roaming_app = tmp_path / "AppData" / "Roaming"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app))
    monkeypatch.setenv("APPDATA", str(roaming_app))

    paths = InstallerPaths.create(home=tmp_path)
    adapter = WindowsPlatformAdapter()

    # Create Chief files
    paths.identity_config.parent.mkdir(parents=True, exist_ok=True)
    paths.identity_config.write_text('{"owner": "admin"}', encoding="utf-8")
    paths.config.write_text('{"db": "production"}', encoding="utf-8")
    paths.caddyfile.write_text('localhost { respond "ok" }', encoding="utf-8")
    (paths.production).mkdir(parents=True, exist_ok=True)
    (paths.production / "chief.sqlite").write_text("db content", encoding="utf-8")
    (paths.caddy_root / "data" / "caddy" / "pki" / "authorities" / "local").mkdir(parents=True, exist_ok=True)
    (paths.caddy_root / "data" / "caddy" / "pki" / "authorities" / "local" / "root.crt").write_text("CA", encoding="utf-8")
    (paths.archive_base / "Archive-1").mkdir(parents=True, exist_ok=True)
    (paths.archive_base / "Archive-1" / "saved.txt").write_text("archive", encoding="utf-8")

    # Create unrelated files
    unrelated_doc = tmp_path / "Desktop" / "farm_tax_return.pdf"
    unrelated_doc.parent.mkdir(parents=True, exist_ok=True)
    unrelated_doc.write_text("Tax Return 2026", encoding="utf-8")

    unrelated_proj = tmp_path / "projects" / "other_app" / "config.json"
    unrelated_proj.parent.mkdir(parents=True, exist_ok=True)
    unrelated_proj.write_text("{}", encoding="utf-8")

    # Mock services as stopped
    monkeypatch.setattr(adapter, "verify_service_active", lambda s: False)
    monkeypatch.setattr(adapter, "get_task_status", lambda s: None)

    from deployment.installer.orchestrator import uninstall_chief
    report = uninstall_chief(paths=paths, platform=adapter, purge=True)

    assert report["success"] is True
    # Chief files must be gone
    assert not paths.identity_config.exists()
    assert not paths.config.exists()
    assert not paths.caddyfile.exists()
    assert not paths.production.exists()
    assert not paths.caddy_root.exists()
    assert not paths.archive_base.exists()

    # Unrelated files must remain untouched
    assert unrelated_doc.exists()
    assert unrelated_doc.read_text(encoding="utf-8") == "Tax Return 2026"
    assert unrelated_proj.exists()
    assert unrelated_proj.read_text(encoding="utf-8") == "{}"


def test_windows_uninstall_fails_if_service_cannot_be_stopped(tmp_path, monkeypatch):
    """Verify uninstall_chief fails closed if a Chief service cannot be stopped."""
    paths = InstallerPaths.create(home=tmp_path)
    adapter = WindowsPlatformAdapter()

    # Mock service as persistently active
    monkeypatch.setattr(adapter, "verify_service_active", lambda s: s == "chief-tls-proxy.service")

    from deployment.installer.errors import InstallerError
    from deployment.installer.orchestrator import uninstall_chief

    with pytest.raises(InstallerError) as exc_info:
        uninstall_chief(paths=paths, platform=adapter, purge=False, service_stop_timeout=0.1)

    assert "Chief service(s) failed to stop" in str(exc_info.value)

