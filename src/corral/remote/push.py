"""推送：等你回答、以及一轮正常/异常结束时，把人叫回手机。

触发口径：

- **等你回答**：关注状态变成 waiting（原有行为）。
- **一轮结束**：SessKit ``status_tag`` 变为已完成 / 已中断（产品方向 2026-09-15）。
  禁止用进程退出或裸「有新消息」当触发——长任务会刷屏，用户很快关掉通知。

因为通道是端到端加密的，中继读不到内容，所以推送发出去的是一层加密壳：手机上的
通知服务扩展在本地解开，再渲染出真实的标题与正文。中继全程只知道「给哪个设备令牌
发一条多大的推送」。

节流：同一会话同一类提醒两分钟内只推一次。助手在等待与执行之间来回抖动、或
status 短时间抖动时，不加节流会把用户口袋里的手机震到没电。

去重：同一会话同一轮结束、同一设备只推一次。去重键是
``(session_key, completion_id, kind, device_id)``——``completion_id`` 来自 SessKit
（同一轮重扫不变，新一轮必变，重启可重算）。内存 120 秒节流只防抖动，
不同轮次不受牵连；已发集合按设备落盘，重启不重推。

可靠性（2026-09-30）：``sender`` 仅入队（中继帧已发出），入队 ≠ APNs 接受。
只有中继回执 ``FRAME_PUSH_RECEIPT ok:true``（APNs HTTP 200 accepted）才记已发；
回执失败 / 回执超时只记失败并保留待重试，同一轮下次扫描重发。
APNs 接受不等于手机已展示——日志一律用 queued / accepted / failed 命名。
旧中继（无回执）永不被当成功：超时后有界重试同一轮最新状态。

Concurrency (2026-09-30 closure): the scanner thread and the asyncio receipt
callback mutate the sent/pending ledgers concurrently. Mutation, snapshot, and
file write are atomic under a single ``_lock`` (flat order — the lock is never
held across ``sender()``), and every write goes through a UNIQUE temp path
before atomic ``os.replace``, so concurrent saves cannot clobber each other or
persist an older snapshot over newer accepted state.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import tempfile
import threading
import time

from sesskit import titles as sesskit_titles

from corral import observe
from corral.remote import crypto
from corral.remote.config import RemoteState, remote_dir

_THROTTLE_SECONDS = 120
_SENT_FILENAME = "push-sent.json"
_SENT_LIMIT = 500
# 待确认回执：push_id -> 记录。落盘，重启后仍可对同一轮最新状态重试。
_PENDING_FILENAME = "push-pending.json"
_PENDING_LIMIT = 200
# Base wait for a missing receipt (old relays, lost receipts): unknown failure,
# retryable. Network errors share this linear bounded backoff.
_RECEIPT_TIMEOUT = 60.0
# Backoff cap: min(60s x attempts, 600s). Bounded linear backoff.
_RECEIPT_BACKOFF_MAX = 600.0
# Apple 5xx earliest retry floor (official provider rules: retry 5xx only after
# 15 minutes). Receipt status is persisted so the rule survives restart.
_APPLE_5XX_RETRY = 900.0
# 同一 (round, device) 最多发送次数；超限后 parked 等新一轮，避免空转。
_MAX_ATTEMPTS = 5
# Permanent failure codes: retrying the same token is pointless, park for a new
# round. Covers Apple's never-retry list (BadDeviceToken, DeviceTokenNotForTopic,
# Forbidden, ExpiredToken, Unregistered, PayloadTooLarge) via relay code mapping.
_PERMANENT_CODES = frozenset({"bad_token", "bad_request", "rejected"})

_KIND_WAITING = "waiting"
_KIND_COMPLETED = "completed"
_KIND_ABORTED = "aborted"


class PushNotifier:
    """把关注状态 / 会话结束状态翻译成加密推送，交给中继投递。"""

    def __init__(
        self,
        state: RemoteState,
        static_private: bytes,
        sender=None,
        *,
        sent_path=None,
        pending_path=None,
    ) -> None:
        self.state = state
        self.static_private = static_private
        self.sender = None  # Injected via set_sender, with receipt self-registration
        self._lock = threading.Lock()
        # Throttle: throttle_key -> (ts, round_key). Same-round repeats within
        # 120s are suppressed; a new completion (new round) escapes the old window.
        self._last_sent: dict[str, tuple[float, str]] = {}
        # Sent set: (session_key, completion_id, kind, device_id) -> timestamp.
        # Persisted; no resend after restart. Legacy keys without a device suffix
        # are honored read-only (no upgrade resend storm).
        # sent_path / pending_path are test-only; production uses remote_dir().
        self._sent_path_override = sent_path
        self._sent_rounds: dict[str, float] = self._load_sent()
        # Pending acks: push_id -> {round, device, kind, session, completion,
        # ts, attempts, last_code, status, parked}.
        self._pending_path_override = pending_path
        self._pending: dict[str, dict] = self._load_pending()
        if sender is not None:
            self.set_sender(sender)

    def set_sender(self, sender) -> None:
        self.sender = sender
        # A relay client with receipt dispatch registers itself, so the daemon
        # assembly needs no change; plain function senders (tests) skip this.
        register = getattr(getattr(sender, "__self__", None), "set_receipt_handler", None)
        if callable(register):
            try:
                register(self.on_push_receipt)
            except Exception:
                pass

    def on_attention_change(self, session: dict, previous: str, current: str) -> None:
        if current != "waiting":
            return
        self._emit(session, kind=_KIND_WAITING)

    def on_status_change(self, session: dict, previous: str, current: str) -> None:
        """SessKit status_tag 跃迁：已完成 / 已中断才推；首扫与同值抖动不推。

        已完成但缺 ``completion_id``（老 SessKit / Cursor 弱证据）时默认不推——
        宁可漏推一次，也不把「刚开了个头」当成干完了。
        """
        # 同标签但 completion_id 变了 = 新一轮结束（SessionHub 在 DONE→DONE
        # 新一轮时也会调 hook，prev 传回当前标签）。_emit 内按轮去重+节流，
        # 同一轮重复调用不会重发。缺 id 的 DONE 保守静默。
        if current == sesskit_titles.STATUS_DONE and (
            previous != sesskit_titles.STATUS_DONE or previous == current
        ):
            if not str(session.get("completion_id") or ""):
                observe.event(
                    "remote_push_skipped_no_completion_id",
                    session=str(session.get("key") or ""),
                    kind=_KIND_COMPLETED,
                )
                return
            self._emit(session, kind=_KIND_COMPLETED)
        elif current == sesskit_titles.STATUS_ABORTED and (
            previous != sesskit_titles.STATUS_ABORTED or previous == current
        ):
            self._emit(session, kind=_KIND_ABORTED)

    @staticmethod
    def _round_key(session: dict, kind: str) -> str:
        key = str(session.get("key") or "")
        completion = str(session.get("completion_id") or "")
        return f"{key}\0{completion or session.get('status', '')}\0{kind}"

    @staticmethod
    def _device_round_key(round_key: str, device_id: str) -> str:
        return f"{round_key}\0{device_id}"

    def _already_sent(self, round_key: str, device_id: str = "") -> bool:
        with self._lock:
            if device_id and self._device_round_key(round_key, device_id) in self._sent_rounds:
                return True
            # Legacy global keys (no device suffix) honored read-only: no resend
            # storm after upgrade.
            return round_key in self._sent_rounds

    def _mark_sent(self, round_key: str, now: float, device_id: str = "") -> None:
        target = self._device_round_key(round_key, device_id) if device_id else round_key
        with self._lock:
            self._sent_rounds[target] = now
            # Bounded LRU: keep the newest N entries; a new round always has a
            # new id so eviction cannot delete the current round.
            if len(self._sent_rounds) > _SENT_LIMIT:
                for old in sorted(self._sent_rounds, key=self._sent_rounds.get)[: len(self._sent_rounds) - _SENT_LIMIT]:
                    del self._sent_rounds[old]
            self._persist_sent_locked()

    def _sent_path(self):
        if self._sent_path_override is not None:
            return self._sent_path_override
        return remote_dir() / _SENT_FILENAME

    def _load_sent(self) -> dict[str, float]:
        try:
            raw = json.loads(self._sent_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, float] = {}
        for key, value in raw.items():
            try:
                out[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        # 启动时只留最近 N 轮，防止旧文件无限膨胀。
        if len(out) > _SENT_LIMIT:
            ordered = sorted(out, key=out.get)[- _SENT_LIMIT:]
            out = {key: out[key] for key in ordered}
        return out

    @staticmethod
    def _write_atomic(path, text: str) -> None:
        """Atomically replace path with text, mode 0600, via a UNIQUE temp file.

        A unique temp path per call means concurrent writers can never truncate
        or replace each other's temp file; the final os.replace is atomic, so
        readers only ever see whole old or whole new content.
        """
        from pathlib import Path as _Path

        path = _Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _persist_sent_locked(self) -> None:
        """Write the CURRENT sent set. Call with _lock held (see class note)."""
        try:
            self._write_atomic(
                self._sent_path(),
                json.dumps(self._sent_rounds, ensure_ascii=False),
            )
        except OSError as exc:
            observe.event("remote_push_sent_save_failed", error=str(exc))

    def _persist_pending_locked(self) -> None:
        """Write the CURRENT pending map. Call with _lock held (see class note)."""
        try:
            self._write_atomic(
                self._pending_path(),
                json.dumps(self._pending, ensure_ascii=False),
            )
        except OSError as exc:
            observe.event("remote_push_pending_save_failed", error=str(exc))

    def _pending_path(self):
        if self._pending_path_override is not None:
            return self._pending_path_override
        return remote_dir() / _PENDING_FILENAME

    def _load_pending(self) -> dict[str, dict]:
        try:
            raw = json.loads(self._pending_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict] = {}
        for push_id, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            try:
                out[str(push_id)] = {
                    "round": str(entry.get("round") or ""),
                    "device": str(entry.get("device") or ""),
                    "kind": str(entry.get("kind") or ""),
                    "session": str(entry.get("session") or ""),
                    "completion": str(entry.get("completion") or ""),
                    "ts": float(entry.get("ts") or 0.0),
                    "attempts": int(entry.get("attempts") or 0),
                    "last_code": str(entry.get("last_code") or ""),
                    "status": int(entry.get("status") or 0),
                    "parked": bool(entry.get("parked")),
                }
            except (TypeError, ValueError):
                continue
        if len(out) > _PENDING_LIMIT:
            ordered = sorted(out, key=lambda k: out[k]["ts"])[-_PENDING_LIMIT:]
            out = {key: out[key] for key in ordered}
        return out

    def _track_pending(self, push_id: str, entry: dict) -> None:
        with self._lock:
            self._pending[push_id] = entry
            if len(self._pending) > _PENDING_LIMIT:
                aged = sorted(self._pending, key=lambda k: self._pending[k]["ts"])
                for old in aged[: len(self._pending) - _PENDING_LIMIT]:
                    del self._pending[old]
            self._persist_pending_locked()

    def _drop_pending(self, push_id: str) -> dict | None:
        with self._lock:
            entry = self._pending.pop(push_id, None)
            if entry is not None:
                self._persist_pending_locked()
            return entry

    def _clear_throttle(self, throttle_key: str) -> None:
        with self._lock:
            self._last_sent.pop(throttle_key, None)

    def _is_parked(self, round_key: str, device_id: str) -> bool:
        with self._lock:
            return any(
                entry.get("parked")
                and entry.get("round") == round_key
                and entry.get("device") == device_id
                for entry in self._pending.values()
            )

    def _inflight(self, round_key: str, device_id: str, exclude: str = "") -> bool:
        """True when the same (round, device) has a fresh, unparked pending entry."""
        now = time.time()
        with self._lock:
            for push_id, entry in self._pending.items():
                if push_id == exclude or entry.get("parked"):
                    continue
                if entry.get("round") == round_key and entry.get("device") == device_id:
                    if now - float(entry.get("ts") or 0.0) < self._backoff_for(entry):
                        return True
            return False

    @staticmethod
    def _backoff_for(entry: dict) -> float:
        # Apple 5xx: earliest retry 900s per official provider rules (persisted
        # status survives restart). Anything else: bounded linear backoff.
        try:
            if int(entry.get("status") or 0) >= 500:
                return _APPLE_5XX_RETRY
        except (TypeError, ValueError):
            pass
        attempts = max(1, int(entry.get("attempts") or 0))
        return min(_RECEIPT_TIMEOUT * attempts, _RECEIPT_BACKOFF_MAX)

    def _send_to_device(
        self,
        session: dict,
        device,
        body: bytes,
        *,
        kind: str,
        round_key: str,
        session_key: str,
        completion: str,
        attempts_base: int,
        replace_push_id: str = "",
        status_base: int = 0,
    ) -> bool:
        """Send one frame to one device: track pending BEFORE calling sender.

        Returns True on enqueue (no exception); a synchronous receipt arriving
        inline still matches the already-persisted pending entry. Send failures
        bump the new entry's ts/last_code and return False. Seal failures
        (deterministic local errors) track nothing and return False.
        """
        try:
            sealed = crypto.seal_for_device(
                self.static_private, bytes.fromhex(device.public_key), body
            )
        except Exception as exc:
            observe.event("remote_push_seal_failed", error=str(exc), kind=kind)
            return False
        push_id = secrets.token_hex(8)
        now = time.time()
        try:
            carried_status = int(status_base or 0)
        except (TypeError, ValueError):
            carried_status = 0
        self._track_pending(push_id, {
            "round": round_key,
            "device": str(device.id or device.push_token),
            "kind": kind,
            "session": session_key,
            "completion": completion,
            "ts": now,
            "attempts": int(attempts_base or 0) + 1,
            "last_code": "",
            "status": carried_status,
            "parked": False,
        })
        if replace_push_id and replace_push_id != push_id:
            self._drop_pending(replace_push_id)
        try:
            self.sender(
                device.push_token,
                device.push_env,
                base64.b64encode(sealed),
                push_id,
            )
        except TypeError:
            # Legacy 3-arg senders (tests / old injections): fall back to
            # receipt-less enqueue. Receipt-less never counts as success: the
            # persisted pending entry is retried by retry_due on backoff.
            try:
                self.sender(device.push_token, device.push_env, base64.b64encode(sealed))
            except Exception as exc:
                self._note_send_error(push_id, kind, session_key, exc)
                return False
        except Exception as exc:
            self._note_send_error(push_id, kind, session_key, exc)
            return False
        return True

    def _note_send_error(self, push_id: str, kind: str, session_key: str, exc: Exception) -> None:
        """Local send failure: keep pending (bump ts/last_code), retryable."""
        with self._lock:
            entry = self._pending.get(push_id)
            if entry is not None:
                entry["ts"] = time.time()
                entry["last_code"] = "local_send"
                self._persist_pending_locked()
        observe.event("remote_push_send_failed", error=str(exc), kind=kind, session=session_key)

    def _emit(self, session: dict, *, kind: str) -> None:
        """Transition-path enqueue: records queued only, never sent.

        Throttle is (ts, round): same-round repeats within 120s are suppressed;
        a new completion (new round) escapes the old window. Devices already
        sent, in-flight, or parked are skipped. Only ``on_push_receipt(ok:true)``
        marks a device sent.
        """
        if self.sender is None:
            return
        key = str(session.get("key") or "")
        if not key:
            return
        if not self._device_wants(session, kind):
            return
        round_key = self._round_key(session, kind)
        throttle_key = f"{key}:{kind}"
        completion = str(session.get("completion_id") or "")
        now = time.time()
        with self._lock:
            last = self._last_sent.get(throttle_key)
            if last is not None:
                last_ts, last_round = last
                if now - last_ts < _THROTTLE_SECONDS and last_round == round_key:
                    # 同轮抖动抑制；新轮不受牵连（下行更新本轮）。
                    return
            self._last_sent[throttle_key] = (now, round_key)
        body = self._render(session, kind=kind)
        queued = 0
        for device in self.state.devices:
            if not device.push_token:
                continue
            if not self._device_allows(device, kind):
                continue
            device_id = str(device.id or device.push_token)
            if self._already_sent(round_key, device_id):
                continue
            if self._is_parked(round_key, device_id):
                continue
            if self._inflight(round_key, device_id):
                continue
            if self._send_to_device(
                session, device, body,
                kind=kind, round_key=round_key, session_key=key,
                completion=completion, attempts_base=0,
            ):
                queued += 1
        if queued:
            observe.event("remote_push_queued", session=key, kind=kind, devices=queued)

    def on_push_receipt(self, push_id: str, receipt: dict) -> None:
        """Handle a relay receipt: only ok marks the device sent.

        Failures keep pending (bump ts/last_code/status, retry after backoff);
        permanent codes (bad_token/bad_request/rejected) park for a new round.
        ``receipt`` looks like ``{"ok","code","status","reason","apns_id"}``.
        Unknown push_ids (late receipts after a timed-out resend) only count.
        """
        receipt = receipt or {}
        pid = str(push_id or "")
        with self._lock:
            entry = self._pending.get(pid)
            snapshot_entry = dict(entry) if entry is not None else None
        if entry is None:
            observe.event("remote_push_receipt_unknown", push_id=pid)
            return
        round_key = snapshot_entry["round"]
        device_id = snapshot_entry["device"]
        kind = snapshot_entry["kind"]
        session_key = snapshot_entry["session"]
        now = time.time()
        status = 0
        try:
            status = int(receipt.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        if receipt.get("ok"):
            self._drop_pending(pid)
            self._mark_sent(round_key, now, device_id)
            observe.event(
                "remote_push_accepted",
                session=session_key,
                kind=kind,
                apns_id=str(receipt.get("apns_id") or ""),
            )
            # Transition alias: dashboards still querying remote_push_sent keep
            # flowing; the semantics are accepted (see module docstring).
            observe.event("remote_push_sent", session=session_key, kind=kind, devices=1, accepted=True)
            return
        code = str(receipt.get("code") or "internal")
        reason = str(receipt.get("reason") or "")
        if code in _PERMANENT_CODES:
            with self._lock:
                parked = self._pending.get(pid)
                if parked is not None:
                    parked["parked"] = True
                    parked["last_code"] = code
                    parked["status"] = status
                    self._persist_pending_locked()
            observe.event(
                "remote_push_failed",
                session=session_key,
                kind=kind,
                code=code,
                status=status,
                reason=reason,
            )
            observe.event("remote_push_parked", session=session_key, kind=kind, code=code)
            return
        with self._lock:
            kept = self._pending.get(pid)
            if kept is not None:
                kept["ts"] = now
                kept["last_code"] = code
                kept["status"] = status
                self._persist_pending_locked()
        self._clear_throttle(f"{session_key}:{kind}")
        observe.event(
            "remote_push_failed",
            session=session_key,
            kind=kind,
            code=code,
            status=status,
            reason=reason,
        )

    def retry_due(self, sessions: list[dict]) -> None:
        """Resend due pending entries still on the newest round (called each scan).

        Targeted per device: each due (round, device) resends exactly one frame;
        accepted siblings never resend, in-flight siblings suppress duplicates,
        and sibling attempts are untouched. A vanished session or a changed
        completion_id expires the old entry; sends beyond ``_MAX_ATTEMPTS`` per
        (round, device) park for a new round (persisted, restart-proof).
        """
        if self.sender is None:
            return
        now = time.time()
        with self._lock:
            due = [
                (push_id, dict(entry))
                for push_id, entry in self._pending.items()
                if not entry.get("parked")
                and now - float(entry.get("ts") or 0.0) >= self._backoff_for(entry)
            ]
        if not due:
            return
        live = {str(s.get("key") or ""): s for s in sessions}
        for push_id, entry in due:
            session = live.get(entry["session"])
            if session is None:
                self._drop_pending(push_id)
                continue
            if str(session.get("completion_id") or "") != entry["completion"]:
                # Superseded by a new round: the new round goes through the
                # normal transition path.
                self._drop_pending(push_id)
                continue
            attempts = int(entry.get("attempts") or 0)
            if attempts >= _MAX_ATTEMPTS:
                with self._lock:
                    parked = self._pending.get(push_id)
                    if parked is not None:
                        parked["parked"] = True
                        self._persist_pending_locked()
                observe.event(
                    "remote_push_parked",
                    session=entry["session"],
                    kind=entry["kind"],
                    attempts=attempts,
                )
                continue
            if self._already_sent(entry["round"], entry["device"]):
                self._drop_pending(push_id)
                continue
            if self._inflight(entry["round"], entry["device"], exclude=push_id):
                continue
            device = self._find_device(entry["device"])
            if device is None:
                self._drop_pending(push_id)
                continue
            # Retries escape the old throttle (scan cadence is the backoff).
            self._clear_throttle(f"{entry['session']}:{entry['kind']}")
            body = self._render(session, kind=entry["kind"])
            ok = self._send_to_device(
                session, device, body,
                kind=entry["kind"], round_key=entry["round"],
                session_key=entry["session"], completion=entry["completion"],
                attempts_base=attempts, replace_push_id=push_id,
                status_base=int(entry.get("status") or 0),
            )
            if not ok:
                # Seal failure (deterministic): no new entry was tracked, so
                # count the attempt on the kept entry to stay bounded.
                with self._lock:
                    kept = self._pending.get(push_id)
                    if kept is not None:
                        kept["ts"] = time.time()
                        kept["attempts"] = attempts + 1
                        self._persist_pending_locked()

    def _find_device(self, device_id: str):
        for device in self.state.devices:
            if str(device.id or device.push_token) == device_id:
                return device
        return None

    def _device_wants(self, session: dict, kind: str) -> bool:
        """任一有 token 且允许该类的设备存在才发；否则连已发都不记。"""
        for device in self.state.devices:
            if device.push_token and self._device_allows(device, kind):
                return True
        return False

    @staticmethod
    def _device_allows(device, kind: str) -> bool:
        if kind == _KIND_COMPLETED:
            return getattr(device, "notify_completed", True) is not False
        if kind == _KIND_ABORTED:
            return getattr(device, "notify_aborted", True) is not False
        return True

    def _render(self, session: dict, *, kind: str) -> bytes:
        """通知的真实内容。手机解开后直接照这几个字段渲染，不需要再回来查一次。"""
        last_agent = str(session.get("last_agent") or "").strip()
        if kind == _KIND_WAITING:
            body = last_agent or "助手在等你回答"
        elif kind == _KIND_ABORTED:
            body = last_agent or "Session stopped with an error"
        else:
            body = last_agent or "Session finished"
        payload = {
            "key": session.get("key"),
            "title": session.get("title") or "会话",
            "runtime": session.get("runtime"),
            "cwd": session.get("cwd_display") or "",
            "body": body,
            "kind": kind,
            "ts": time.time(),
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
