"""State machine and transitions for Chief Unified Installer."""
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from .errors import CancelledError, InstallerError
from .events import EventBus
from .models import StepDefinition, StepId, StepStatus

STEP_DEFINITIONS: Dict[StepId, StepDefinition] = {
    StepId.IDLE: StepDefinition(StepId.IDLE, "Installer Ready", "Waiting to start installation", idempotent=True, retryable=True),
    StepId.PREFLIGHT: StepDefinition(StepId.PREFLIGHT, "Checking Computer", "Verifying system requirements and capabilities", idempotent=True, retryable=True),
    StepId.RELEASE_VALIDATION: StepDefinition(StepId.RELEASE_VALIDATION, "Validating Release", "Checking release manifest and component commits", idempotent=True, retryable=False),
    StepId.EXISTING_INSTALL_DETECTION: StepDefinition(StepId.EXISTING_INSTALL_DETECTION, "Scanning Existing Installs", "Checking for previous Chief installations", idempotent=True, retryable=True),
    StepId.ARCHIVE: StepDefinition(StepId.ARCHIVE, "Archiving Previous Chief", "Safely archiving earlier deployments without data loss", idempotent=False, retryable=False),
    StepId.SYSTEM_DEPENDENCIES: StepDefinition(StepId.SYSTEM_DEPENDENCIES, "Preparing System", "Installing required operating-system dependencies", idempotent=True, retryable=True),
    StepId.RUNTIME: StepDefinition(StepId.RUNTIME, "Installing Chief Runtime", "Creating isolated private Python runtime and dependencies", idempotent=True, retryable=True),
    StepId.BROWSER: StepDefinition(StepId.BROWSER, "Installing Browser Engine", "Installing qualified headless browser runtime", idempotent=True, retryable=True),
    StepId.PWA_RUNTIME: StepDefinition(StepId.PWA_RUNTIME, "Preparing Node.js Environment", "Installing verified private Node.js builder", idempotent=True, retryable=True),
    StepId.PWA_BUILDS: StepDefinition(StepId.PWA_BUILDS, "Building Apps", "Building Owner, Staff, and Job web applications", idempotent=True, retryable=True),
    StepId.IDENTITY: StepDefinition(StepId.IDENTITY, "Configuring Identity", "Configuring Chief ownership and host identity", idempotent=True, retryable=False),
    StepId.PRODUCTION_INSTANCE: StepDefinition(StepId.PRODUCTION_INSTANCE, "Preparing Production State", "Bootstrapping verified production database", idempotent=False, retryable=False),
    StepId.BACKUP: StepDefinition(StepId.BACKUP, "Creating Initial Backup", "Creating verified disaster-recovery snapshot", idempotent=True, retryable=False),
    StepId.MANAGED_INSTALL: StepDefinition(StepId.MANAGED_INSTALL, "Bootstrapping Managed Slot", "Creating atomic managed installation slot", idempotent=False, retryable=False),
    StepId.READINESS: StepDefinition(StepId.READINESS, "Running Readiness Gate", "Verifying installation integrity against readiness rules", idempotent=True, retryable=False),
    StepId.SERVICES: StepDefinition(StepId.SERVICES, "Configuring Services", "Installing systemd background user services", idempotent=True, retryable=True),
    StepId.TLS: StepDefinition(StepId.TLS, "Installing HTTPS Proxy", "Installing verified Caddy server and local TLS", idempotent=True, retryable=True),
    StepId.LOCAL_DISCOVERY: StepDefinition(StepId.LOCAL_DISCOVERY, "Configuring Local Discovery", "Advertising local mDNS hostname across network", idempotent=True, retryable=True),
    StepId.HEALTH_CHECK: StepDefinition(StepId.HEALTH_CHECK, "Verifying Health", "Starting services and validating health endpoints", idempotent=True, retryable=True),
    StepId.DEVICE_SETUP: StepDefinition(StepId.DEVICE_SETUP, "Preparing Mobile Pairing", "Exporting local CA certificates for phones", idempotent=True, retryable=True),
    StepId.COMPLETE: StepDefinition(StepId.COMPLETE, "Installation Complete", "Chief is ready to use", idempotent=True, retryable=False),
    StepId.FAILED: StepDefinition(StepId.FAILED, "Installation Failed", "Installation encountered an unrecoverable issue", idempotent=True, retryable=True),
    StepId.CANCELLED: StepDefinition(StepId.CANCELLED, "Installation Cancelled", "Installation was stopped by user request", idempotent=True, retryable=False),
}

ORDERED_INSTALL_STEPS: List[StepId] = [
    StepId.PREFLIGHT,
    StepId.RELEASE_VALIDATION,
    StepId.EXISTING_INSTALL_DETECTION,
    StepId.ARCHIVE,
    StepId.SYSTEM_DEPENDENCIES,
    StepId.RUNTIME,
    StepId.BROWSER,
    StepId.PWA_RUNTIME,
    StepId.PWA_BUILDS,
    StepId.IDENTITY,
    StepId.PRODUCTION_INSTANCE,
    StepId.BACKUP,
    StepId.MANAGED_INSTALL,
    StepId.READINESS,
    StepId.SERVICES,
    StepId.TLS,
    StepId.LOCAL_DISCOVERY,
    StepId.HEALTH_CHECK,
    StepId.DEVICE_SETUP,
    StepId.COMPLETE,
]


def _build_valid_transitions() -> Dict[StepId, Set[StepId]]:
    transitions: Dict[StepId, Set[StepId]] = {
        StepId.IDLE: {StepId.PREFLIGHT, StepId.CANCELLED},
    }
    for i, step in enumerate(ORDERED_INSTALL_STEPS):
        allowed: Set[StepId] = {StepId.FAILED, StepId.CANCELLED}
        if i + 1 < len(ORDERED_INSTALL_STEPS):
            allowed.add(ORDERED_INSTALL_STEPS[i + 1])
        # Allow retry of self
        allowed.add(step)
        transitions[step] = allowed

    transitions[StepId.COMPLETE] = {StepId.COMPLETE}
    transitions[StepId.FAILED] = {StepId.IDLE, StepId.PREFLIGHT}  # Allow restart after failure
    transitions[StepId.CANCELLED] = {StepId.IDLE}
    return transitions


VALID_TRANSITIONS: Dict[StepId, Set[StepId]] = _build_valid_transitions()


class StateMachine:
    """Explicit installer state machine enforcing valid transitions and emitting events."""

    def __init__(self, event_bus: Optional[EventBus] = None):
        self.event_bus = event_bus or EventBus()
        self.current_step = StepId.IDLE
        self.current_status = StepStatus.PENDING
        self.history: List[Tuple[StepId, StepStatus, str, str]] = []
        self._step_start_times: Dict[StepId, str] = {}
        self._is_cancelled = False

    @property
    def is_cancelled(self) -> bool:
        return self._is_cancelled

    def request_cancellation(self) -> None:
        self._is_cancelled = True

    def can_transition(self, target_step: StepId) -> bool:
        allowed = VALID_TRANSITIONS.get(self.current_step, set())
        return target_step in allowed

    def transition(
        self,
        target_step: StepId,
        status: StepStatus,
        *,
        message: str = "",
        progress: float = 0.0,
        error_code: Optional[str] = None,
        retryable: bool = False,
    ) -> None:
        if not self.can_transition(target_step):
            raise InstallerError(
                f"Invalid installer transition from {self.current_step.value} to {target_step.value}.",
                code="INVALID_STATE_TRANSITION",
            )

        now_iso = datetime.now(timezone.utc).isoformat()
        started_at = self._step_start_times.get(target_step)
        finished_at = None

        if status == StepStatus.RUNNING:
            started_at = now_iso
            self._step_start_times[target_step] = now_iso
        elif status in (StepStatus.SUCCESS, StepStatus.FAILED, StepStatus.SKIPPED):
            finished_at = now_iso

        self.current_step = target_step
        self.current_status = status
        self.history.append((target_step, status, message, now_iso))

        step_def = STEP_DEFINITIONS.get(target_step)
        display_name = step_def.display_name if step_def else target_step.value

        self.event_bus.emit(
            step_id=target_step,
            display_name=display_name,
            status=status,
            progress=progress,
            message=message,
            error_code=error_code,
            retryable=retryable,
            started_at=started_at,
            finished_at=finished_at,
        )

    def fail(self, error: Exception, *, step_id: Optional[StepId] = None) -> None:
        failing_step = step_id or self.current_step
        code = getattr(error, "code", "INSTALLATION_FAILED")
        retryable = getattr(error, "retryable", False)
        msg = str(error)

        self.transition(
            StepId.FAILED,
            StepStatus.FAILED,
            message=f"Failed during {failing_step.value}: {msg}",
            error_code=code,
            retryable=retryable,
        )

    def cancel(self, message: str = "Installation cancelled by user.") -> None:
        self._is_cancelled = True
        self.transition(
            StepId.CANCELLED,
            StepStatus.SKIPPED,
            message=message,
        )
