"""Structured installer events and event bus."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from typing import Callable, List, Optional

from .models import StepId, StepStatus


@dataclass
class InstallEvent:
    step_id: StepId
    display_name: str
    status: StepStatus
    progress: float = 0.0  # 0.0 to 1.0
    message: str = ""
    error_code: Optional[str] = None
    retryable: bool = False
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["step_id"] = self.step_id.value
        d["status"] = self.status.value
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


EventListener = Callable[[InstallEvent], None]


class EventBus:
    """Publish-subscribe bus for installer progress and status events."""

    def __init__(self):
        self._listeners: List[EventListener] = []

    def subscribe(self, listener: EventListener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unsubscribe(self, listener: EventListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def emit(
        self,
        step_id: StepId,
        display_name: str,
        status: StepStatus,
        *,
        progress: float = 0.0,
        message: str = "",
        error_code: Optional[str] = None,
        retryable: bool = False,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
    ) -> InstallEvent:
        now_iso = datetime.now(timezone.utc).isoformat()
        if status == StepStatus.RUNNING and not started_at:
            started_at = now_iso
        elif status in (StepStatus.SUCCESS, StepStatus.FAILED, StepStatus.SKIPPED) and not finished_at:
            finished_at = now_iso

        event = InstallEvent(
            step_id=step_id,
            display_name=display_name,
            status=status,
            progress=progress,
            message=message,
            error_code=error_code,
            retryable=retryable,
            started_at=started_at,
            finished_at=finished_at,
        )
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                # Listeners must never crash the installer state machine
                pass
        return event
