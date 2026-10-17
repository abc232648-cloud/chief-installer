"""Packaged Windows executable acceptance tests.

Validates the self-contained native PE executable (Chief-Installer-Windows.exe) built for Windows.
Verifies PE binary integrity, SHA-256 metadata, windowed configuration, and CLI dispatch.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows executable acceptance tests run only on Windows")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SCRIPT = _PROJECT_ROOT / "packaging" / "installer" / "windows" / "build_package.py"
_DIST_EXE = _PROJECT_ROOT / "dist" / "installer" / "windows" / "Chief-Installer-Windows.exe"
_DIST_META = _PROJECT_ROOT / "dist" / "installer" / "windows" / "INSTALLER_SHA256.json"
_DIST_SHA256 = _PROJECT_ROOT / "dist" / "installer" / "windows" / "Chief-Installer-Windows.exe.sha256"


def _ensure_package() -> Path:
    """Return existing packaged Windows executable or build it."""
    if _DIST_EXE.exists() and _DIST_META.exists():
        return _DIST_EXE

    has_pyinstaller = bool(
        shutil.which("pyinstaller")
        or (Path(sys.executable).parent / ("pyinstaller.exe" if os.name == "nt" else "pyinstaller")).exists()
    )
    if not has_pyinstaller:
        pytest.skip("Packaged Windows executable not yet built and PyInstaller not installed; skipping")

    spec = importlib.util.spec_from_file_location("win_build_package", str(_BUILD_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_package(_PROJECT_ROOT / "dist" / "installer" / "windows")


def _run_artifact(artifact: Path, *args: str, env: dict = None) -> subprocess.CompletedProcess:
    cmd = [str(artifact), *args]
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, env=run_env)


def test_windows_packaged_artifact_integrity():
    """Verify package executable exists, is PE binary, and matches SHA-256 metadata."""
    artifact = _ensure_package()
    assert artifact.exists(), "Packaged Windows artifact executable not found"
    assert artifact.is_file()

    # PE header check (MZ)
    assert artifact.read_bytes()[:2] == b"MZ", "Executable does not have MZ header"

    assert _DIST_META.exists(), "INSTALLER_SHA256.json metadata file not found"
    meta = json.loads(_DIST_META.read_text(encoding="utf-8"))
    assert meta["artifact"] == "Chief-Installer-Windows.exe"
    assert meta["release_id"] == "chief-2026-10-16-accepted"
    assert len(meta["sha256"]) == 64

    actual_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert actual_sha == meta["sha256"], f"SHA-256 mismatch: {actual_sha} vs {meta['sha256']}"

    assert _DIST_SHA256.exists(), "Chief-Installer-Windows.exe.sha256 companion not found"
    sha_line = _DIST_SHA256.read_text(encoding="utf-8").strip()
    assert sha_line.startswith(actual_sha)


def test_windows_packaged_artifact_help():
    """Verify packaged executable runs --help with correct usage and options."""
    artifact = _ensure_package()
    proc = _run_artifact(artifact, "--help")
    assert proc.returncode == 0
    # In windowed GUI app, stdout is piped when captured
    combined = proc.stdout + proc.stderr
    assert "chief-installer" in combined
    assert "--owner-name" in combined
    assert "--username" in combined
    assert "--farm-operations" in combined
    assert "--tls-mode" in combined
    assert "--non-interactive" in combined
    assert "--dns-mode" in combined


def test_windows_packaged_artifact_fail_closed_validation():
    """Verify packaged executable fails closed on missing mandatory options in non-interactive mode."""
    artifact = _ensure_package()
    proc = _run_artifact(artifact, "--non-interactive")
    assert proc.returncode == 1
    combined = proc.stdout + proc.stderr
    assert "--owner-name is required" in combined


def test_windows_packaged_artifact_invalid_manifest():
    """Verify packaged executable rejects invalid manifest file path."""
    artifact = _ensure_package()
    proc = _run_artifact(artifact, "--manifest", "non-existent-manifest.json", "--non-interactive")
    assert proc.returncode != 0


def test_windows_spec_is_windowed():
    """Verify PyInstaller spec defines console=False and proper target name."""
    spec_path = _PROJECT_ROOT / "packaging" / "installer" / "windows" / "chief_installer_windows.spec"
    assert spec_path.exists()
    content = spec_path.read_text(encoding="utf-8")
    assert "console=False" in content
    assert "name='Chief-Installer-Windows'" in content
