#!/usr/bin/env bash
# ==============================================================================
# Chief Unified Installer — Packaged Linux Artifact Qualification
#
# This CI-safe script validates the built Linux installer artifact itself:
# package presence, SHA-256 integrity, CLI availability, and fail-closed
# argument validation.
#
# IMPORTANT: This is NOT a real-host Xubuntu installation acceptance test. It
# does not claim to validate live systemd user services, Avahi/mDNS discovery,
# Caddy TLS startup, or the deployed /api/health endpoint. Those checks require
# a separate acceptance run on a real supported Xubuntu host.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
PACKAGE_DIR="$PROJECT_ROOT/dist/installer/linux"
PACKAGE_BIN="$PACKAGE_DIR/chief-installer"

echo "=== Chief Linux Packaged Artifact Qualification ==="

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

# 5. Test Console smoke test
echo "Testing Console smoke test..."
"$PACKAGE_BIN" --console-smoke-test >/dev/null
echo "[OK] --console-smoke-test verified"

echo "=== Packaged artifact qualification passed successfully ==="
echo "NOTE: Real-host Xubuntu service/mDNS/TLS/health acceptance remains a separate release gate."
