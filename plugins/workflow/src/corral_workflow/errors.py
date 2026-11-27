"""Structured error and exit-code contract for ``corral workflow``."""

from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_AUTH = 4
EXIT_CONFLICT = 5
EXIT_TIMEOUT = 6

ENVELOPE_VERSION = 1


class WorkflowError(Exception):
    """Agent-friendly CLI error carrying stable code, hint, context and recovery commands."""

    def __init__(
        self,
        code: str,
        message: str,
        exit_code: int = EXIT_ERROR,
        *,
        hint: str | None = None,
        next_commands: list[str] | None = None,
        retryable: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.hint = hint
        self.next_commands = list(next_commands or [])
        self.retryable = bool(retryable)
        self.context = dict(context) if context else None

    def to_error_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.next_commands:
            payload["next_commands"] = list(self.next_commands)
        if self.context is not None:
            payload["context"] = self.context
        return payload

    def to_envelope(self, *, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        envelope_meta = {"version": ENVELOPE_VERSION}
        if meta:
            envelope_meta.update(meta)
        return {
            "ok": False,
            "data": None,
            "error": self.to_error_dict(),
            "meta": envelope_meta,
        }


def ok_envelope(data: Any, *, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    envelope_meta = {"version": ENVELOPE_VERSION}
    if meta:
        envelope_meta.update(meta)
    return {
        "ok": True,
        "data": data,
        "error": None,
        "meta": envelope_meta,
    }
