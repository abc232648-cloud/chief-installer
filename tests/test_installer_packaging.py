import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import deployment.installer.__main__ as installer_main

_BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "installer" / "linux" / "build_package.py"
_spec = importlib.util.spec_from_file_location("linux_build_package", str(_BUILD_SCRIPT))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

build_package = _mod.build_package
sha256_file = _mod.sha256_file


def test_linux_package_build_and_metadata(tmp_path):
    output_dir = tmp_path / "package_out"
    artifact = build_package(output_dir)

    assert artifact.exists()
    assert artifact.is_file()
    assert artifact.name == "chief-installer"

    # Verify SHA-256 metadata file
    meta_path = output_dir / "INSTALLER_SHA256.json"
    assert meta_path.exists()

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["artifact"] == "chief-installer"
    assert meta["release_id"] == "chief-2026-10-16-accepted"
    assert meta["sha256"] == sha256_file(artifact)
    assert meta["size_bytes"] == artifact.stat().st_size


def _run_artifact(artifact: Path, *args: str) -> subprocess.CompletedProcess:
    with open(artifact, "rb") as f:
        header = f.read(4)
    is_native = header.startswith(b"\x7fELF") or header.startswith(b"MZ")
    cmd = [str(artifact), *args] if is_native else [sys.executable, str(artifact), *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_frozen_entrypoint_uses_host_python_for_child_modules(tmp_path, monkeypatch):
    """Regression: the PyInstaller binary itself must never become `python -m`."""
    fake_installer = tmp_path / "chief-installer"
    fake_python = tmp_path / "python3"
    fake_installer.write_text("installer", encoding="utf-8")
    fake_python.write_text("python", encoding="utf-8")

    monkeypatch.setattr(installer_main.sys, "frozen", True, raising=False)
    monkeypatch.setattr(installer_main.sys, "executable", str(fake_installer))
    monkeypatch.delenv("CHIEF_INSTALLER_FROZEN_EXECUTABLE", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI-chief-installer")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)

    def fake_which(name):
        return str(fake_python) if name == "python3" else None

    monkeypatch.setattr(installer_main.shutil, "which", fake_which)

    installer_main._configure_packaged_python()

    assert os.environ["CHIEF_INSTALLER_FROZEN_EXECUTABLE"] == str(fake_installer)
    assert installer_main.sys.executable == str(fake_python)
    assert "LD_LIBRARY_PATH" not in os.environ


def test_frozen_entrypoint_restores_original_loader_path_for_children(tmp_path, monkeypatch):
    """External children must not inherit PyInstaller's private native libraries."""
    fake_installer = tmp_path / "chief-installer"
    fake_python = tmp_path / "python3"
    fake_installer.write_text("installer", encoding="utf-8")
    fake_python.write_text("python", encoding="utf-8")

    monkeypatch.setattr(installer_main.sys, "frozen", True, raising=False)
    monkeypatch.setattr(installer_main.sys, "executable", str(fake_installer))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI-chief-installer:/usr/local/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/lib:/opt/chief-host-lib")

    def fake_which(name):
        return str(fake_python) if name == "python3" else None

    monkeypatch.setattr(installer_main.shutil, "which", fake_which)

    installer_main._configure_packaged_python()

    assert os.environ["LD_LIBRARY_PATH"] == "/usr/local/lib:/opt/chief-host-lib"
    assert installer_main.sys.executable == str(fake_python)


def test_source_entrypoint_does_not_modify_loader_path(monkeypatch):
    """Source execution must preserve the user's normal process environment."""
    monkeypatch.delattr(installer_main.sys, "frozen", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/custom/user/lib")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)

    installer_main._configure_packaged_python()

    assert os.environ["LD_LIBRARY_PATH"] == "/custom/user/lib"


def test_frozen_entrypoint_routes_internal_privileged_helper(monkeypatch):
    captured = {}

    def fake_helper(args):
        captured["args"] = list(args)
        return 23

    monkeypatch.setattr(installer_main, "_run_internal_privileged_helper", fake_helper)
    monkeypatch.setattr(
        installer_main.sys,
        "argv",
        ["chief-installer", "--internal-privileged-helper", "enable-linger", "ape-xubuntu"],
    )

    assert installer_main.main() == 23
    assert captured["args"] == ["enable-linger", "ape-xubuntu"]


def test_packaged_executable_runs_help(tmp_path):
    output_dir = tmp_path / "package_out"
    artifact = build_package(output_dir)

    proc = _run_artifact(artifact, "--help")
    assert proc.returncode == 0
    assert "chief-installer" in proc.stdout
    assert "--owner-name" in proc.stdout
    assert "--farm-operations" in proc.stdout


def test_packaged_executable_non_interactive_validation(tmp_path):
    output_dir = tmp_path / "package_out"
    artifact = build_package(output_dir)

    proc = _run_artifact(artifact, "--non-interactive")
    assert proc.returncode == 1
    assert "--owner-name is required" in proc.stderr


def test_desktop_file_contract():
    root = Path(__file__).resolve().parents[1]
    desktop_file = root / "packaging" / "installer" / "linux" / "chief-installer.desktop"
    assert desktop_file.exists()

    content = desktop_file.read_text(encoding="utf-8")
    assert "[Desktop Entry]" in content
    assert "Type=Application" in content
    assert "Name=Chief Installer" in content
    assert "Exec=chief-installer" in content
    assert "Terminal=false" in content
