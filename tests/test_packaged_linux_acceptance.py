"""Packaged Linux artifact acceptance tests.

Validates the self-contained native executable package built for Linux (Xubuntu reference platform).
Verifies package integrity, command-line interface contracts, fail-closed parameter validation,
and packaged helper routing without relying only on source-level tests.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SCRIPT = _PROJECT_ROOT / "packaging" / "installer" / "linux" / "build_package.py"
_DIST_EXE = _PROJECT_ROOT / "dist" / "installer" / "linux" / "chief-installer"
_DIST_META = _PROJECT_ROOT / "dist" / "installer" / "linux" / "INSTALLER_SHA256.json"


def _ensure_package() -> Path:
    """Build or return existing packaged artifact."""
    if _DIST_EXE.exists() and _DIST_META.exists():
        return _DIST_EXE

    spec = importlib.util.spec_from_file_location("linux_build_package", str(_BUILD_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_package(_PROJECT_ROOT / "dist" / "installer" / "linux")


def _run_artifact(artifact: Path, *args: str, env: dict = None) -> subprocess.CompletedProcess:
    with open(artifact, "rb") as f:
        header = f.read(4)
    is_native = header.startswith(b"\x7fELF") or header.startswith(b"MZ")
    cmd = [str(artifact), *args] if is_native else [sys.executable, str(artifact), *args]
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, env=run_env)


def test_packaged_artifact_integrity():
    """Verify package binary exists, is executable, and matches SHA-256 metadata."""
    artifact = _ensure_package()
    assert artifact.exists(), "Packaged artifact executable not found"
    assert artifact.is_file()

    assert _DIST_META.exists(), "INSTALLER_SHA256.json metadata file not found"
    meta = json.loads(_DIST_META.read_text(encoding="utf-8"))
    assert meta["artifact"] == "chief-installer"
    assert meta["release_id"] == "chief-2026-10-16-accepted"
    assert len(meta["sha256"]) == 64

    import hashlib
    actual_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert actual_sha == meta["sha256"], f"SHA-256 mismatch: {actual_sha} vs {meta['sha256']}"


def test_packaged_artifact_help_interface():
    """Verify packaged executable runs standalone --help with correct options."""
    artifact = _ensure_package()
    proc = _run_artifact(artifact, "--help")
    assert proc.returncode == 0
    assert "chief-installer" in proc.stdout
    assert "--owner-name" in proc.stdout
    assert "--username" in proc.stdout
    assert "--farm-operations" in proc.stdout
    assert "--tls-mode" in proc.stdout
    assert "--non-interactive" in proc.stdout


def test_packaged_artifact_internal_helper_is_routed_before_cli():
    """Regression for the physical Folio failure: frozen helper re-entry must not hit argparse."""
    artifact = _ensure_package()
    proc = _run_artifact(
        artifact,
        "--internal-privileged-helper",
        "definitely-forbidden-test-command",
    )
    assert proc.returncode == 2
    assert "Unknown or forbidden privileged command" in proc.stderr
    assert "unrecognized arguments" not in proc.stderr


def test_packaged_artifact_fail_closed_validation():
    """Verify packaged executable strictly fails closed on missing mandatory options."""
    artifact = _ensure_package()
    proc = _run_artifact(artifact, "--non-interactive")
    assert proc.returncode == 1
    assert "--owner-name is required" in proc.stderr


def test_packaged_artifact_invalid_manifest():
    """Verify packaged executable rejects invalid manifest file path."""
    artifact = _ensure_package()
    proc = _run_artifact(artifact, "--manifest", "non-existent-manifest.json", "--non-interactive")
    assert proc.returncode != 0


def test_packaged_desktop_integration():
    """Verify desktop entry specification for Xubuntu application menu."""
    desktop_file = _PROJECT_ROOT / "packaging" / "installer" / "linux" / "chief-installer.desktop"
    assert desktop_file.exists()
    content = desktop_file.read_text(encoding="utf-8")
    assert "[Desktop Entry]" in content
    assert "Type=Application" in content
    assert "Name=Chief Installer" in content
    assert "Exec=/opt/chief-installer/chief-installer --gui" in content
    assert "Terminal=false" in content
    assert "Categories=System;Setup;" in content
