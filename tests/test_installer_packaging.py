import importlib.util
import json
from pathlib import Path
import subprocess
import sys

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


def test_packaged_executable_runs_help(tmp_path):
    output_dir = tmp_path / "package_out"
    artifact = build_package(output_dir)

    # Execute built package with python interpreter
    proc = subprocess.run([sys.executable, str(artifact), "--help"], capture_output=True, text=True)
    assert proc.returncode == 0
    assert "chief-installer" in proc.stdout
    assert "--owner-name" in proc.stdout
    assert "--farm-operations" in proc.stdout


def test_packaged_executable_non_interactive_validation(tmp_path):
    output_dir = tmp_path / "package_out"
    artifact = build_package(output_dir)

    # In non-interactive mode without --owner-name, exits 1 with clean error
    proc = subprocess.run([sys.executable, str(artifact), "--non-interactive"], capture_output=True, text=True)
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
