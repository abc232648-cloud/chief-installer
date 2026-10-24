#!/usr/bin/env python3
"""Chief Unified Installer — Packaged Windows Artifact Qualification.

Validates the built Windows installer executable (Chief-Installer-Windows.exe):
presence, PE binary header, SHA-256 integrity, CLI interface dispatch,
and fail-closed parameter validation.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[2]
PACKAGE_DIR = PROJECT_ROOT / "dist" / "installer" / "windows"
PACKAGE_BIN = PACKAGE_DIR / "Chief-Installer-Windows.exe"
META_FILE = PACKAGE_DIR / "INSTALLER_SHA256.json"
SHA_FILE = PACKAGE_DIR / "Chief-Installer-Windows.exe.sha256"


def main() -> int:
    global PACKAGE_BIN, META_FILE, SHA_FILE
    if len(sys.argv) > 1:
        PACKAGE_BIN = Path(sys.argv[1]).resolve()
        META_FILE = PACKAGE_BIN.parent / "INSTALLER_SHA256.json"
        SHA_FILE = PACKAGE_BIN.parent / (PACKAGE_BIN.name + ".sha256")

    print("=== Chief Windows Packaged Artifact Qualification ===")

    # 1. Ensure package exists or build it
    if not PACKAGE_BIN.exists():
        print("Building Windows package artifact...")
        build_script = SCRIPT_DIR / "build_package.py"
        res = subprocess.run([sys.executable, str(build_script)], cwd=str(PROJECT_ROOT))
        if res.returncode != 0:
            print(f"ERROR: Build script failed with exit code {res.returncode}", file=sys.stderr)
            return 1

    if not PACKAGE_BIN.exists():
        print(f"ERROR: Package executable {PACKAGE_BIN} not found!", file=sys.stderr)
        return 1

    print(f"[OK] Found packaged installer executable at: {PACKAGE_BIN}")

    # 2. Check PE binary header
    header = PACKAGE_BIN.read_bytes()[:2]
    if header != b"MZ":
        print(f"ERROR: Invalid PE header (expected b'MZ', got {header!r})", file=sys.stderr)
        return 1
    print("[OK] Valid Windows PE executable header verified (MZ)")

    # 3. Verify SHA-256 integrity
    print("Verifying SHA-256 checksum...")
    if not META_FILE.exists():
        print(f"ERROR: Metadata file {META_FILE} not found!", file=sys.stderr)
        return 1

    meta = json.loads(META_FILE.read_text(encoding="utf-8"))
    actual_sha = hashlib.sha256(PACKAGE_BIN.read_bytes()).hexdigest()
    if actual_sha != meta.get("sha256"):
        print(f"ERROR: Checksum mismatch: {actual_sha} vs {meta.get('sha256')}", file=sys.stderr)
        return 1
    print(f"  Checksum verified: {actual_sha}")

    if SHA_FILE.exists():
        sha_line = SHA_FILE.read_text(encoding="utf-8").strip()
        if not sha_line.startswith(actual_sha):
            print(f"ERROR: Companion .sha256 mismatch: {sha_line} vs {actual_sha}", file=sys.stderr)
            return 1
        print("  Companion .sha256 file verified")

    # 4. Test CLI interface (--help)
    print("Testing CLI options (--help)...")
    res = subprocess.run([str(PACKAGE_BIN), "--help"], capture_output=True, text=True)
    if res.returncode != 0:
        print(f"ERROR: --help exited with code {res.returncode}: {res.stderr}", file=sys.stderr)
        return 1
    output = res.stdout + res.stderr
    if "chief-installer" not in output and "--owner-name" not in output:
        print(f"ERROR: --help output missing expected usage text: {output[:200]}", file=sys.stderr)
        return 1
    print("[OK] --help verified successfully")

    # 5. Test fail-closed validation on missing arguments
    print("Testing fail-closed parameter validation...")
    res = subprocess.run([str(PACKAGE_BIN), "--non-interactive"], capture_output=True, text=True)
    if res.returncode == 0:
        print("ERROR: Expected failure when --owner-name missing, but succeeded!", file=sys.stderr)
        return 1
    print("[OK] Fail-closed argument validation verified (exit code != 0)")

    # 6. Test Console smoke test
    print("Testing Console smoke dispatch (--console-smoke-test)...")
    res = subprocess.run([str(PACKAGE_BIN), "--console-smoke-test"], capture_output=True, text=True)
    if res.returncode != 0:
        print(f"ERROR: --console-smoke-test failed with code {res.returncode}: {res.stderr}", file=sys.stderr)
        return 1
    if "smoke test passed" not in res.stdout.lower():
        print(f"ERROR: Expected smoke test confirmation, got: {res.stdout}", file=sys.stderr)
        return 1
    print("[OK] --console-smoke-test verified successfully")

    print("=== Packaged Windows artifact qualification passed successfully ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
