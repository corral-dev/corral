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
"""

from __future__ import annotations

import base64
import json
import secrets
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
# 无回执即视为未知失败、可重试的等待秒数（覆盖旧中继与回执丢失）。
_RECEIPT_TIMEOUT = 60.0
# 同一 (round, device) 最多重发次数；超限后挂起等新一轮，避免对旧中继空转。
_MAX_ATTEMPTS = 5

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
        self.sender = None  # 经 set_sender 注入，附带回执自注册
        self._lock = threading.Lock()
        self._last_sent: dict[str, float] = {}
        # 已发集合：(session_key, completion_id, kind, device_id) -> 发送时间。
        # 落盘，重启不重推。无 device 后缀的旧键只读兼容（升级后不重推风暴）。
        # sent_path / pending_path 仅测试注入；生产默认走 remote_dir()。
        self._sent_path_override = sent_path
        self._sent_rounds: dict[str, float] = self._load_sent()
        # 待确认：push_id -> {round, device, kind, session, completion, ts, attempts}。
        self._pending_path_override = pending_path
        self._pending: dict[str, dict] = self._load_pending()
        if sender is not None:
            self.set_sender(sender)

    def set_sender(self, sender) -> None:
        self.sender = sender
        # 中继客户端自带回执分发时自动挂上，无需改 daemon 组装层；
        # 普通函数型 sender（测试）没有该方法则跳过。
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
            # 旧版全局键（无设备后缀）只读兼容：升级后不重推风暴。
            return round_key in self._sent_rounds

    def _mark_sent(self, round_key: str, now: float, device_id: str = "") -> None:
        target = self._device_round_key(round_key, device_id) if device_id else round_key
        with self._lock:
            self._sent_rounds[target] = now
            # 有界 LRU：只留最近 N 条，旧轮自然淘汰（新一轮 id 必变，不会误删）。
            if len(self._sent_rounds) > _SENT_LIMIT:
                for old in sorted(self._sent_rounds, key=self._sent_rounds.get)[: len(self._sent_rounds) - _SENT_LIMIT]:
                    del self._sent_rounds[old]
            snapshot = dict(self._sent_rounds)
        self._save_sent(snapshot)

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

    def _save_sent(self, snapshot: dict[str, float]) -> None:
        try:
            path = self._sent_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            try:
                tmp.chmod(0o600)
            except OSError:
                pass
            tmp.replace(path)
        except OSError as exc:
            observe.event("remote_push_sent_save_failed", error=str(exc))

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
                }
            except (TypeError, ValueError):
                continue
        if len(out) > _PENDING_LIMIT:
            ordered = sorted(out, key=lambda k: out[k]["ts"])[-_PENDING_LIMIT:]
            out = {key: out[key] for key in ordered}
        return out

    def _save_pending(self, snapshot: dict[str, dict]) -> None:
        try:
            path = self._pending_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            try:
                tmp.chmod(0o600)
            except OSError:
                pass
            tmp.replace(path)
        except OSError as exc:
            observe.event("remote_push_pending_save_failed", error=str(exc))

    def _track_pending(self, push_id: str, entry: dict) -> None:
        with self._lock:
            self._pending[push_id] = entry
            if len(self._pending) > _PENDING_LIMIT:
                aged = sorted(self._pending, key=lambda k: self._pending[k]["ts"])
                for old in aged[: len(self._pending) - _PENDING_LIMIT]:
                    del self._pending[old]
            snapshot = dict(self._pending)
        self._save_pending(snapshot)

    def _drop_pending(self, push_id: str) -> dict | None:
        with self._lock:
            entry = self._pending.pop(push_id, None)
            snapshot = dict(self._pending)
        if entry is not None:
            self._save_pending(snapshot)
        return entry

    def _clear_throttle(self, throttle_key: str) -> None:
        with self._lock:
            self._last_sent.pop(throttle_key, None)

    def _emit(self, session: dict, *, kind: str, attempts_base: int = 0) -> None:
        """入队一轮通知：只记 queued，永不记已发。

        已发只由 ``on_push_receipt(ok:true)`` 按设备标记；回执失败 / 超时 /
        本地发送异常都不记，留待 ``retry_due`` 对同一轮最新状态重试。
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
        now = time.time()
        with self._lock:
            if now - self._last_sent.get(throttle_key, 0.0) < _THROTTLE_SECONDS:
                # 节流只跳过本次发送，不记已发：同一轮下次扫描仍可推。
                return
            self._last_sent[throttle_key] = now
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
            try:
                sealed = crypto.seal_for_device(
                    self.static_private, bytes.fromhex(device.public_key), body
                )
            except Exception as exc:
                observe.event("remote_push_seal_failed", error=str(exc), kind=kind)
                continue
            push_id = secrets.token_hex(8)
            try:
                self.sender(
                    device.push_token,
                    device.push_env,
                    base64.b64encode(sealed),
                    push_id,
                )
            except TypeError:
                # 旧式三参数 sender（测试 / 旧注入）：回退为无回执入队。
                # 无回执永不算成功：记一条带回执超时的待确认，由 retry_due 重试。
                try:
                    self.sender(device.push_token, device.push_env, base64.b64encode(sealed))
                except Exception as exc:
                    observe.event("remote_push_send_failed", error=str(exc), kind=kind, session=key)
                    continue
            except Exception as exc:
                # 发送失败不记已发：同一轮下次扫描仍可重试；轮次已变则自然过期。
                observe.event("remote_push_send_failed", error=str(exc), kind=kind, session=key)
                continue
            self._track_pending(push_id, {
                "round": round_key,
                "device": device_id,
                "kind": kind,
                "session": key,
                "completion": str(session.get("completion_id") or ""),
                "ts": now,
                "attempts": int(attempts_base or 0) + 1,
            })
            queued += 1
        if queued:
            observe.event("remote_push_queued", session=key, kind=kind, devices=queued)

    def on_push_receipt(self, push_id: str, receipt: dict) -> None:
        """处理中继回执：ok 才按设备记已发；失败清节流留待重试。

        ``receipt`` 形如 ``{"ok","code","status","reason","apns_id"}``。
        未知 push_id（超时重发后的迟到回执等）只计数，不记不改。
        """
        receipt = receipt or {}
        entry = self._drop_pending(str(push_id or ""))
        if entry is None:
            observe.event("remote_push_receipt_unknown", push_id=str(push_id or ""))
            return
        round_key = entry["round"]
        device_id = entry["device"]
        kind = entry["kind"]
        session_key = entry["session"]
        throttle_key = f"{session_key}:{kind}"
        now = time.time()
        if receipt.get("ok"):
            self._mark_sent(round_key, now, device_id)
            observe.event(
                "remote_push_accepted",
                session=session_key,
                kind=kind,
                apns_id=str(receipt.get("apns_id") or ""),
            )
            # 过渡别名：老看板仍在查 remote_push_sent 时不断流；语义已是 accepted。
            observe.event("remote_push_sent", session=session_key, kind=kind, devices=1, accepted=True)
            return
        code = str(receipt.get("code") or "internal")
        self._clear_throttle(throttle_key)
        observe.event(
            "remote_push_failed",
            session=session_key,
            kind=kind,
            code=code,
            status=int(receipt.get("status") or 0),
            reason=str(receipt.get("reason") or ""),
        )

    def retry_due(self, sessions: list[dict]) -> None:
        """重发仍是最新轮次且回执超时/失败的待确认（SessionHub 每轮扫描后调用）。

        只重发当前仍是已结束、且 ``completion_id`` 与待确认一致的轮次；
        会话已消失或已有新一轮时旧待确认直接丢弃。同一 (round, device)
        超过 ``_MAX_ATTEMPTS`` 后挂起等新轮次，避免对旧中继空转。
        """
        if self.sender is None:
            return
        now = time.time()
        with self._lock:
            due = [
                (push_id, dict(entry))
                for push_id, entry in self._pending.items()
                if now - float(entry.get("ts") or 0.0) >= _RECEIPT_TIMEOUT
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
                # 已有新一轮：旧待确认过期，新轮由正常跃迁路径推送。
                self._drop_pending(push_id)
                continue
            if int(entry.get("attempts") or 0) >= _MAX_ATTEMPTS:
                self._drop_pending(push_id)
                observe.event(
                    "remote_push_parked",
                    session=entry["session"],
                    kind=entry["kind"],
                    attempts=int(entry.get("attempts") or 0),
                )
                continue
            if self._already_sent(entry["round"], entry["device"]):
                self._drop_pending(push_id)
                continue
            self._drop_pending(push_id)
            # 重试不受旧节流牵连（扫描间隔本身即退避；新轮次本就不受牵连）。
            self._clear_throttle(f"{entry['session']}:{entry['kind']}")
            self._emit(session, kind=entry["kind"], attempts_base=int(entry.get("attempts") or 0))

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
