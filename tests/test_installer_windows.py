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
