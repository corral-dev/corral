"""Durable host receipts for remote input commands (Slice A).

Unique key = authenticated device identity + command_id. Same key with the same
payload digest returns the existing receipt; a different digest is rejected.
``unknown`` is never auto-converted to rejected or delivered.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from corral.remote import config as remote_config
from corral.remote import crypto

STATUS_UNSEEN = "unseen"
STATUS_ACCEPTED = "accepted"
STATUS_DISPATCHING = "dispatching"
STATUS_DELIVERED = "delivered"
STATUS_REJECTED = "rejected"
STATUS_UNKNOWN = "unknown"

RECEIPT_STATUSES = frozenset(
    {
        STATUS_ACCEPTED,
        STATUS_DISPATCHING,
        STATUS_DELIVERED,
        STATUS_REJECTED,
        STATUS_UNKNOWN,
    }
)

DEFAULT_LEASE_SEC = 30
MAX_LEASE_SEC = 300
# Soft admission ceiling: refuse new accepts when unresolved+recent resolved fill the store.
MAX_RECEIPTS = 10_000
RESOLVED_RETENTION_SEC = 7 * 24 * 60 * 60

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def receipts_dir() -> Path:
    return remote_config.remote_dir() / "command_receipts"


def canonical_digest(payload: dict[str, Any]) -> str:
    """SHA-256 hex of a sorted, separators-tight JSON payload."""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def digest_for_text(*, key: str, text: str, submit: bool) -> str:
    return canonical_digest({"m": "input.text", "key": key, "text": text, "submit": bool(submit)})


def digest_for_keys(*, key: str, keys: list[str]) -> str:
    return canonical_digest({"m": "input.keys", "key": key, "keys": list(keys)})


def digest_for_image(*, key: str, image_bytes: bytes) -> str:
    return canonical_digest(
        {
            "m": "input.image",
            "key": key,
            "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
        }
    )


def _safe_segment(value: str) -> str:
    cleaned = _SAFE_ID_RE.sub("_", value.strip())[:120]
    return cleaned or "x"


@dataclass
class Receipt:
    command_id: str
    device_key: str
    status: str
    host_run_id: str
    payload_digest: str
    target_key: str = ""
    method: str = ""
    reason: str | None = None
    # Human-readable host cause (localized ActionError text) for rejected and
    # unknown receipts. The wire name is the established ``detail`` field the
    # phone already decodes; a ``message`` alias with the same text is also
    # emitted for compatibility. Absent when the host has no human cause
    # (internal store transitions such as host_restart).
    detail: str | None = None
    retryable: bool | None = None
    lease_expires_mono: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    accepted_host_run_id: str = ""

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "command_id": self.command_id,
            "status": self.status,
            "host_run_id": self.host_run_id,
        }
        if self.reason is not None:
            out["reason"] = self.reason
        if self.detail is not None:
            out["detail"] = self.detail
            out["message"] = self.detail
        if self.retryable is not None:
            out["retryable"] = self.retryable
        return out

    def to_record(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "device_key": self.device_key,
            "status": self.status,
            "host_run_id": self.host_run_id,
            "payload_digest": self.payload_digest,
            "target_key": self.target_key,
            "method": self.method,
            "reason": self.reason,
            "detail": self.detail,
            "message": self.detail,
            "retryable": self.retryable,
            "lease_expires_mono": self.lease_expires_mono,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "accepted_host_run_id": self.accepted_host_run_id or self.host_run_id,
        }

    @classmethod
    def from_record(cls, raw: dict[str, Any]) -> Receipt:
        return cls(
            command_id=str(raw.get("command_id") or ""),
            device_key=str(raw.get("device_key") or ""),
            status=str(raw.get("status") or STATUS_UNKNOWN),
            host_run_id=str(raw.get("host_run_id") or ""),
            payload_digest=str(raw.get("payload_digest") or ""),
            target_key=str(raw.get("target_key") or ""),
            method=str(raw.get("method") or ""),
            reason=raw.get("reason"),
            # New records carry ``detail``; accept the legacy ``message`` alias.
            detail=raw.get("detail", raw.get("message")),
            retryable=raw.get("retryable"),
            lease_expires_mono=float(raw.get("lease_expires_mono") or 0.0),
            created_at=float(raw.get("created_at") or 0.0),
            updated_at=float(raw.get("updated_at") or 0.0),
            accepted_host_run_id=str(raw.get("accepted_host_run_id") or raw.get("host_run_id") or ""),
        )


def unseen_receipt(*, command_id: str, host_run_id: str) -> dict[str, Any]:
    return {
        "command_id": command_id,
        "status": STATUS_UNSEEN,
        "host_run_id": host_run_id,
        "retryable": True,
    }


class CommandReceiptStore:
    """Process-local store with durable JSON files under the remote state directory."""

    def __init__(self, host_run_id: str, *, root: Path | None = None) -> None:
        self.host_run_id = host_run_id
        self._root = root
        self._lock = threading.RLock()
        self._reconcile_on_open()

    def _base(self) -> Path:
        base = self._root if self._root is not None else receipts_dir()
        base.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(base, 0o700)
        except OSError:
            pass
        return base

    def _path(self, device_key: str, command_id: str) -> Path:
        return self._base() / _safe_segment(device_key) / f"{_safe_segment(command_id)}.json"

    def _read(self, path: Path) -> Receipt | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        return Receipt.from_record(raw)

    def _write(self, receipt: Receipt) -> None:
        path = self._path(receipt.device_key, receipt.command_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        text = json.dumps(receipt.to_record(), ensure_ascii=False, indent=2, sort_keys=True)
        # Local atomic replace; config helper is private and also recreates remote_dir.
        tmp = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, text.encode("utf-8"))
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _iter_paths(self) -> list[Path]:
        base = self._base()
        if not base.is_dir():
            return []
        return sorted(base.glob("*/*.json"))

    def _count(self) -> int:
        return len(self._iter_paths())

    def _reconcile_on_open(self) -> None:
        """On a new host run, interrupt accepted and mark in-flight dispatching unknown."""
        now = time.time()
        with self._lock:
            for path in self._iter_paths():
                receipt = self._read(path)
                if receipt is None:
                    continue
                if receipt.status in (STATUS_DELIVERED, STATUS_REJECTED, STATUS_UNKNOWN):
                    # Drop aged resolved receipts to keep admission room.
                    if (
                        receipt.status in (STATUS_DELIVERED, STATUS_REJECTED)
                        and receipt.updated_at
                        and now - receipt.updated_at > RESOLVED_RETENTION_SEC
                    ):
                        try:
                            path.unlink()
                        except OSError:
                            pass
                    continue
                if receipt.accepted_host_run_id == self.host_run_id:
                    continue
                if receipt.status == STATUS_ACCEPTED:
                    receipt.status = STATUS_REJECTED
                    receipt.reason = "interrupted"
                    receipt.retryable = False
                    receipt.host_run_id = self.host_run_id
                    receipt.updated_at = now
                    self._write(receipt)
                elif receipt.status == STATUS_DISPATCHING:
                    receipt.status = STATUS_UNKNOWN
                    receipt.reason = "host_restart"
                    receipt.retryable = False
                    receipt.host_run_id = self.host_run_id
                    receipt.updated_at = now
                    self._write(receipt)

    def get(self, device_key: str, command_id: str) -> Receipt | None:
        with self._lock:
            return self._read(self._path(device_key, command_id))

    def status(self, device_key: str, command_id: str) -> dict[str, Any]:
        receipt = self.get(device_key, command_id)
        if receipt is None:
            return unseen_receipt(command_id=command_id, host_run_id=self.host_run_id)
        wire = receipt.to_wire()
        # Status queries always report the current process run id.
        wire["host_run_id"] = self.host_run_id
        return wire

    def begin(
        self,
        *,
        device_key: str,
        command_id: str,
        payload_digest: str,
        target_key: str,
        method: str,
        lease_sec: float = DEFAULT_LEASE_SEC,
    ) -> tuple[Receipt, bool]:
        """Claim or return an existing receipt.

        Returns ``(receipt, is_new)``. ``is_new`` is True only when this call
        durably committed ``accepted`` and the caller may proceed to dispatch.
        """
        if not command_id.strip():
            raise ValueError("command_id required")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", payload_digest):
            raise ValueError("payload_digest must be sha256 hex")

        lease_sec = max(1.0, min(float(lease_sec), float(MAX_LEASE_SEC)))
        now_wall = time.time()
        now_mono = time.monotonic()

        with self._lock:
            existing = self.get(device_key, command_id)
            if existing is not None:
                if existing.payload_digest != payload_digest:
                    # Keep the original durable receipt; reject this attempt only.
                    rejected = Receipt(
                        command_id=command_id,
                        device_key=device_key,
                        status=STATUS_REJECTED,
                        host_run_id=self.host_run_id,
                        payload_digest=payload_digest,
                        target_key=target_key,
                        method=method,
                        reason="digest_mismatch",
                        retryable=False,
                        created_at=now_wall,
                        updated_at=now_wall,
                        accepted_host_run_id=self.host_run_id,
                    )
                    return rejected, False
                # Same digest: return existing without re-dispatch.
                wire = Receipt.from_record(existing.to_record())
                wire.host_run_id = self.host_run_id
                return wire, False

            if self._count() >= MAX_RECEIPTS:
                rejected = Receipt(
                    command_id=command_id,
                    device_key=device_key,
                    status=STATUS_REJECTED,
                    host_run_id=self.host_run_id,
                    payload_digest=payload_digest,
                    target_key=target_key,
                    method=method,
                    reason="capacity",
                    retryable=True,
                    created_at=now_wall,
                    updated_at=now_wall,
                    accepted_host_run_id=self.host_run_id,
                )
                return rejected, False

            receipt = Receipt(
                command_id=command_id,
                device_key=device_key,
                status=STATUS_ACCEPTED,
                host_run_id=self.host_run_id,
                payload_digest=payload_digest,
                target_key=target_key,
                method=method,
                lease_expires_mono=now_mono + lease_sec,
                created_at=now_wall,
                updated_at=now_wall,
                accepted_host_run_id=self.host_run_id,
            )
            # Durable accept BEFORE any external side effect.
            self._write(receipt)
            return receipt, True

    def mark_dispatching(self, receipt: Receipt) -> Receipt:
        with self._lock:
            if receipt.status != STATUS_ACCEPTED:
                return receipt
            if time.monotonic() > receipt.lease_expires_mono:
                receipt.status = STATUS_REJECTED
                receipt.reason = "lease_expired"
                receipt.retryable = False
                receipt.host_run_id = self.host_run_id
                receipt.updated_at = time.time()
                self._write(receipt)
                return receipt
            receipt.status = STATUS_DISPATCHING
            receipt.host_run_id = self.host_run_id
            receipt.updated_at = time.time()
            self._write(receipt)
            return receipt

    def mark_delivered(self, receipt: Receipt) -> Receipt:
        with self._lock:
            if receipt.status in (STATUS_REJECTED, STATUS_UNKNOWN, STATUS_DELIVERED):
                return receipt
            receipt.status = STATUS_DELIVERED
            receipt.reason = None
            receipt.retryable = None
            receipt.host_run_id = self.host_run_id
            receipt.updated_at = time.time()
            self._write(receipt)
            return receipt

    def mark_rejected(
        self,
        receipt: Receipt,
        *,
        reason: str,
        retryable: bool,
        detail: str | None = None,
    ) -> Receipt:
        with self._lock:
            if receipt.status in (STATUS_DELIVERED, STATUS_UNKNOWN):
                # Never auto-convert unknown/delivered into rejected.
                # Unknown stays uncertain: callers must not re-mark it here.
                return receipt
            receipt.status = STATUS_REJECTED
            receipt.reason = reason
            if detail is not None:
                receipt.detail = detail
            receipt.retryable = retryable
            receipt.host_run_id = self.host_run_id
            receipt.updated_at = time.time()
            self._write(receipt)
            return receipt

    def mark_unknown(
        self,
        receipt: Receipt,
        *,
        reason: str = "ambiguous",
        detail: str | None = None,
    ) -> Receipt:
        with self._lock:
            if receipt.status in (STATUS_DELIVERED, STATUS_REJECTED, STATUS_UNKNOWN):
                return receipt
            receipt.status = STATUS_UNKNOWN
            receipt.reason = reason
            if detail is not None:
                receipt.detail = detail
            receipt.retryable = False
            receipt.host_run_id = self.host_run_id
            receipt.updated_at = time.time()
            self._write(receipt)
            return receipt


def new_host_run_id() -> str:
    return crypto.random_id(16)
