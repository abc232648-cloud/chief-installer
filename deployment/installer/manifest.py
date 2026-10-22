"""Release manifest contract and validation for Chief Unified Installer."""
from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
from typing import Dict, Union

from .errors import ManifestValidationError, ReleaseMismatchError

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+.*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ReleaseManifest:
    release_id: str
    chief_agent_commit: str
    owner_pwa_commit: str
    staff_pwa_commit: str
    job_pwa_commit: str
    node_version: str
    caddy_version: str
    caddy_checksum: str
    supported_installer_schema: int = 1

    def to_dict(self) -> dict:
        return {
            "release_id": self.release_id,
            "chief_agent_commit": self.chief_agent_commit,
            "owner_pwa_commit": self.owner_pwa_commit,
            "staff_pwa_commit": self.staff_pwa_commit,
            "job_pwa_commit": self.job_pwa_commit,
            "node_version": self.node_version,
            "caddy_version": self.caddy_version,
            "caddy_checksum": self.caddy_checksum,
            "supported_installer_schema": self.supported_installer_schema,
        }

    def verify_component_commit(self, component: str, actual_commit: str) -> None:
        """Verify that a checked-out component commit exactly matches the pinned manifest commit."""
        expected = getattr(self, f"{component}_commit", None)
        if not expected:
            raise ManifestValidationError(f"Unknown component in release manifest: {component}")
        actual_clean = (actual_commit or "").strip().lower()
        if actual_clean != expected.lower():
            raise ReleaseMismatchError(
                f"Release verification failure: {component} commit mismatch. "
                f"Expected {expected}, but found {actual_clean}. Refusing to install unpinned or floating revision.",
                details={"component": component, "expected": expected, "actual": actual_clean},
            )


def parse_manifest(data: Union[str, bytes, dict, Path]) -> ReleaseManifest:
    """Parse and strictly validate a release manifest."""
    if isinstance(data, Path):
        try:
            raw_text = data.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except Exception as exc:
            raise ManifestValidationError(f"Failed to read manifest file {data}: {exc}") from exc
    elif isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except Exception as exc:
            raise ManifestValidationError(f"Invalid JSON in release manifest: {exc}") from exc

    if not isinstance(data, dict):
        raise ManifestValidationError("Release manifest root must be a JSON object.")

    required_fields = {
        "release_id",
        "chief_agent_commit",
        "owner_pwa_commit",
        "staff_pwa_commit",
        "job_pwa_commit",
        "node_version",
        "caddy_version",
        "caddy_checksum",
        "supported_installer_schema",
    }
    missing = required_fields - set(data.keys())
    if missing:
        raise ManifestValidationError(f"Missing required fields in release manifest: {sorted(missing)}")

    if data["supported_installer_schema"] != 1:
        raise ManifestValidationError(
            f"Unsupported installer schema version: {data['supported_installer_schema']}. Supported schema is 1."
        )

    release_id = str(data["release_id"]).strip()
    if not _RELEASE_ID_RE.fullmatch(release_id):
        raise ManifestValidationError(f"Invalid release_id format: {release_id!r}")

    for field_name in ("chief_agent_commit", "owner_pwa_commit", "staff_pwa_commit", "job_pwa_commit"):
        commit = str(data[field_name]).strip().lower()
        if not _COMMIT_RE.fullmatch(commit):
            raise ManifestValidationError(f"Invalid git commit SHA for {field_name}: {data[field_name]!r}")

    node_version = str(data["node_version"]).strip()
    if not _SEMVER_RE.fullmatch(node_version):
        raise ManifestValidationError(f"Invalid node_version format: {node_version!r}")

    caddy_version = str(data["caddy_version"]).strip()
    if not _SEMVER_RE.fullmatch(caddy_version):
        raise ManifestValidationError(f"Invalid caddy_version format: {caddy_version!r}")

    caddy_checksum = str(data["caddy_checksum"]).strip().lower()
    if not _SHA256_RE.fullmatch(caddy_checksum):
        raise ManifestValidationError(f"Invalid caddy_checksum SHA-256: {data['caddy_checksum']!r}")

    return ReleaseManifest(
        release_id=release_id,
        chief_agent_commit=data["chief_agent_commit"].strip().lower(),
        owner_pwa_commit=data["owner_pwa_commit"].strip().lower(),
        staff_pwa_commit=data["staff_pwa_commit"].strip().lower(),
        job_pwa_commit=data["job_pwa_commit"].strip().lower(),
        node_version=node_version,
        caddy_version=caddy_version,
        caddy_checksum=caddy_checksum,
        supported_installer_schema=int(data["supported_installer_schema"]),
    )


# Accepted first release manifest pinning verified baseline commits
ACCEPTED_FIRST_RELEASE_MANIFEST = ReleaseManifest(
    release_id="chief-2026-10-16-accepted",
    chief_agent_commit="6ab701097b774fb3ea14513b164cdf05f74b7bf9",
    owner_pwa_commit="b8d139b0789918f3c937e640cc618e66194c966b",
    staff_pwa_commit="4a07d324e13630e2742a4e69127569d70a3cdf83",
    job_pwa_commit="90eb0cfa2155c4d5f6a4ca8bf510a5d01e1f7db9",
    node_version="22.23.3",
    caddy_version="2.11.7",
    caddy_checksum="5d27b76b95f496638d79208c55250317b8755fd9e90005f7466aa32956944a90",
    supported_installer_schema=1,
)


# Candidate used by this installer build. Historical acceptance above is immutable.
# Review status is explicit in release_id; packaging is not deployment approval.
CURRENT_RELEASE_MANIFEST = replace(
    ACCEPTED_FIRST_RELEASE_MANIFEST,
    release_id="chief-2026-10-20-integrated-review",
    chief_agent_commit="0b08a94bd568ff31e09fe1306d3c2700c54478e5",
    node_version="24.21.0",
)

DEFAULT_RELEASE_MANIFEST = CURRENT_RELEASE_MANIFEST
