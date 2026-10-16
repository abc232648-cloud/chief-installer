"""Data models for the Chief Unified Installer."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class StepId(str, Enum):
    IDLE = "IDLE"
    PREFLIGHT = "PREFLIGHT"
    RELEASE_VALIDATION = "RELEASE_VALIDATION"
    EXISTING_INSTALL_DETECTION = "EXISTING_INSTALL_DETECTION"
    ARCHIVE = "ARCHIVE"
    SYSTEM_DEPENDENCIES = "SYSTEM_DEPENDENCIES"
    RUNTIME = "RUNTIME"
    BROWSER = "BROWSER"
    PWA_RUNTIME = "PWA_RUNTIME"
    PWA_BUILDS = "PWA_BUILDS"
    IDENTITY = "IDENTITY"
    PRODUCTION_INSTANCE = "PRODUCTION_INSTANCE"
    BACKUP = "BACKUP"
    MANAGED_INSTALL = "MANAGED_INSTALL"
    READINESS = "READINESS"
    SERVICES = "SERVICES"
    TLS = "TLS"
    LOCAL_DISCOVERY = "LOCAL_DISCOVERY"
    HEALTH_CHECK = "HEALTH_CHECK"
    DEVICE_SETUP = "DEVICE_SETUP"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class StepDefinition:
    step_id: StepId
    display_name: str
    description: str = ""
    idempotent: bool = True
    retryable: bool = True


@dataclass
class InstallOptions:
    """Collected installation options.

    Passwords are never exposed in string representations or serialized logs.
    """
    owner_name: str
    owner_username: str = "owner"
    owner_password: str = ""
    farm_operations: str = "DISABLED"  # DISABLED or ENABLED
    organization_name: Optional[str] = None
    farm_name: Optional[str] = None
    public_host: Optional[str] = None
    tls_mode: str = "LOCAL"  # LOCAL or PUBLIC
    allow_test_mock: bool = False

    def __repr__(self) -> str:
        return (
            f"InstallOptions(owner_name={self.owner_name!r}, owner_username={self.owner_username!r}, "
            f"farm_operations={self.farm_operations!r}, organization_name={self.organization_name!r}, "
            f"farm_name={self.farm_name!r}, public_host={self.public_host!r}, tls_mode={self.tls_mode!r}, "
            f"allow_test_mock={self.allow_test_mock}, password_set={'YES' if bool(self.owner_password) else 'NO'})"
        )

    def __str__(self) -> str:
        return self.__repr__()


@dataclass
class PlatformInfo:
    os_name: str  # linux, windows, darwin
    distro_id: str  # xubuntu, ubuntu, kali, debian, etc.
    distro_name: str
    distro_version: str
    architecture: str
    is_supported: bool
    details: Dict[str, str] = field(default_factory=dict)


@dataclass
class PreflightCheck:
    name: str
    passed: bool
    message: str
    required: bool = True
    details: Optional[dict] = None


@dataclass
class PreflightReport:
    platform: PlatformInfo
    checks: List[PreflightCheck]
    passed: bool
    existing_install_detected: bool = False
    existing_paths: List[str] = field(default_factory=list)


@dataclass
class InstallResult:
    status: str  # SUCCESS, FAILED, CANCELLED
    public_host: Optional[str] = None
    urls: Dict[str, str] = field(default_factory=dict)
    archive_path: Optional[str] = None
    log_path: Optional[str] = None
    duration_seconds: float = 0.0
    error: Optional[str] = None
    error_code: Optional[str] = None
