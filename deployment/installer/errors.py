"""Error taxonomy for the Chief Unified Installer."""
from typing import Optional


class InstallerError(Exception):
    """Base error for all installer failures."""

    def __init__(self, message: str, code: str = "INSTALLER_ERROR", retryable: bool = False, details: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class PreflightError(InstallerError):
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message, code="PREFLIGHT_FAILED", retryable=False, details=details)


class ManifestValidationError(InstallerError):
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message, code="MANIFEST_INVALID", retryable=False, details=details)


class ReleaseMismatchError(InstallerError):
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message, code="RELEASE_MISMATCH", retryable=False, details=details)


class PrivilegeError(InstallerError):
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message, code="PRIVILEGE_ERROR", retryable=True, details=details)


class SystemDependencyError(InstallerError):
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message, code="SYSTEM_DEPENDENCY_FAILED", retryable=True, details=details)


class RuntimeSetupError(InstallerError):
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message, code="RUNTIME_SETUP_FAILED", retryable=True, details=details)


class BrowserSetupError(InstallerError):
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message, code="BROWSER_SETUP_FAILED", retryable=True, details=details)


class PwaBuildError(InstallerError):
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message, code="PWA_BUILD_FAILED", retryable=True, details=details)


class ProductionPrepareError(InstallerError):
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message, code="PRODUCTION_PREPARE_FAILED", retryable=False, details=details)


class ReadinessGateError(InstallerError):
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message, code="READINESS_GATE_FAILED", retryable=False, details=details)


class ServiceError(InstallerError):
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message, code="SERVICE_ERROR", retryable=True, details=details)


class TlsError(InstallerError):
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message, code="TLS_ERROR", retryable=True, details=details)


class UnsupportedPlatformError(InstallerError):
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message, code="UNSUPPORTED_PLATFORM", retryable=False, details=details)


class CancelledError(InstallerError):
    def __init__(self, message: str = "Installation cancelled by user."):
        super().__init__(message, code="CANCELLED", retryable=False)
