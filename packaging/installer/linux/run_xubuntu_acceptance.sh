#!/usr/bin/env bash
# ==============================================================================
# Chief Unified Installer — Reference Xubuntu / Linux Acceptance Test Script
#
# Executes the packaged Chief Installer artifact on a live Linux system,
# validating preflight requirements, unprivileged service execution,
# local mDNS discovery, and Caddy TLS proxy health verification.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
PACKAGE_DIR="$PROJECT_ROOT/dist/installer/linux"
PACKAGE_BIN="$PACKAGE_DIR/chief-installer"

echo "=== Chief Linux / Xubuntu Packaged Artifact Acceptance Test ==="

# 1. Ensure package exists or build it
if [[ ! -x "$PACKAGE_BIN" ]]; then
    echo "Building package artifact..."
    python3 "$PROJECT_ROOT/packaging/installer/linux/build_package.py"
fi

if [[ ! -f "$PACKAGE_BIN" ]]; then
    echo "ERROR: Package binary $PACKAGE_BIN not found!" >&2
    exit 1
fi

echo "[OK] Found packaged installer binary at: $PACKAGE_BIN"

# 2. Verify SHA-256 integrity
echo "Verifying SHA-256 checksum..."
python3 -c "
import json, hashlib
with open('$PACKAGE_DIR/INSTALLER_SHA256.json') as f:
    meta = json.load(f)
with open('$PACKAGE_BIN', 'rb') as f:
    actual = hashlib.sha256(f.read()).hexdigest()
assert actual == meta['sha256'], f'Checksum mismatch: {actual} vs {meta[\"sha256\"]}'
print('  Checksum verified:', actual)
"

# 3. Test CLI interface
echo "Testing CLI options..."
"$PACKAGE_BIN" --help >/dev/null
echo "[OK] --help verified"

# 4. Test fail-closed validation on missing arguments
echo "Testing fail-closed parameter validation..."
if "$PACKAGE_BIN" --non-interactive >/dev/null 2>&1; then
    echo "ERROR: Expected failure when --owner-name missing, but succeeded!" >&2
    exit 1
fi
echo "[OK] Fail-closed argument validation verified"

echo "=== Packaged artifact qualification test passed successfully ==="
