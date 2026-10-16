"""Sanitized structured logging for the Chief Unified Installer."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

_SECRET_PATTERNS = [
    (re.compile(r"(password\s*=\s*['\"])[^'\"]*(['\"])", re.IGNORECASE), r"\1[REDACTED]\2"),
    (re.compile(r"(--password[= ])\S+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(token[=:])\S+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(key[=:])\S+", re.IGNORECASE), r"\1[REDACTED]"),
]


def sanitize_text(text: str) -> str:
    """Strip passwords, bearer tokens, and secrets from any log string."""
    if not text:
        return ""
    sanitized = text
    for pattern, replacement in _SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


class InstallerLogger:
    """Manages dedicated, sanitized installer session logs and diagnostics."""

    def __init__(self, log_dir: Optional[Path] = None, session_id: Optional[str] = None):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.session_id = session_id or f"installer_{stamp}"
        if log_dir is None:
            state_root = Path.home() / ".local" / "state" / "chief" / "installer" / "logs"
        else:
            state_root = Path(log_dir)
        state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log_file = state_root / f"{self.session_id}.log"
        self.events_file = state_root / f"{self.session_id}.events.jsonl"
        self._entries: List[Dict[str, Any]] = []

    def log(self, level: str, message: str, **kwargs) -> None:
        clean_msg = sanitize_text(message)
        now_iso = datetime.now(timezone.utc).isoformat()
        clean_kwargs = {k: sanitize_text(str(v)) for k, v in kwargs.items()}
        entry = {
            "timestamp": now_iso,
            "level": level.upper(),
            "message": clean_msg,
            **clean_kwargs,
        }
        self._entries.append(entry)

        line = f"[{now_iso}] [{level.upper()}] {clean_msg}"
        if clean_kwargs:
            line += f" | {clean_kwargs}"
        line += "\n"

        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def log_event(self, event_dict: dict) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with open(self.events_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"timestamp": now_iso, **event_dict}) + "\n")
        except Exception:
            pass

    def info(self, message: str, **kwargs) -> None:
        self.log("INFO", message, **kwargs)

    def warning(self, message: str, **kwargs) -> None:
        self.log("WARN", message, **kwargs)

    def error(self, message: str, **kwargs) -> None:
        self.log("ERROR", message, **kwargs)

    def generate_diagnostic_report(self, failed_step: Optional[str] = None, error: Optional[Exception] = None) -> str:
        """Produce a sanitized JSON diagnostic report safe for user export/sharing."""
        report = {
            "session_id": self.session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "failed_step": failed_step,
            "error_type": type(error).__name__ if error else None,
            "error_message": sanitize_text(str(error)) if error else None,
            "error_code": getattr(error, "code", None),
            "log_path": str(self.log_file),
            "recent_entries": self._entries[-30:],
        }
        return json.dumps(report, indent=2)
