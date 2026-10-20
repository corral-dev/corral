"""Detached per-run supervisor: admit, deliver once, observe, settle.

One process per run, owning the run lock for its lifetime and the target lock
while it may type into the session. Input reaches the runtime only through
``SessionHub`` (Corral's hosted terminals); progress and finality come from
SessKit activity plus Corral's own working/screen signals.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import sys
import time
from contextlib import contextmanager

from corral.control import (
    build_result,
    compact_event,
    correlate,
    fresh_plan,
    resolve_target,
    wire_prompt,
)
from corral.control_store import SETTLED, ControlError, RunStore, digest

_POLL = 1.0
_HEARTBEAT = 5.0
_BIND_REFRESH = 3.0
_BUSY_POLL = 3.0
# Inferred (non-native) finality needs a quiet history, an idle screen and no
# working signal for this long; native completion evidence settles at once.
_SETTLE_QUIET = 8.0
_DISPATCH_TIMEOUT = 150.0
_FINAL = {"completed": "completed", "failed": "failed", "rejected": "failed", "interrupted": "interrupted"}


@contextmanager
def file_lock(store: RunStore, identity: str, *, blocking: bool = False):
    store.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = store.root / "locks" / (digest(identity)[:32] + ".lock")
    path.parent.mkdir(exist_ok=True, mode=0o700)
    with path.open("a+") as stream:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(stream.fileno(), flags)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def lock_held(store: RunStore, identity: str) -> bool:
    path = store.root / "locks" / (digest(identity)[:32] + ".lock")
    if not path.exists():
        return False
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return False


def _screen_hash(name: str | None) -> str | None:
    if not name:
        return None
    from corral import embed

    text = embed.capture(name, 0, 0)
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest() if text is not None else None


def _phase(session: dict) -> str:
    """Corral's own working/waiting evidence; unknown never blocks settlement."""
    try:
        from corral.attention_signals import inspect_session

        return str(inspect_session(session).phase or "unknown")
    except Exception:  # noqa: BLE001 - a broken probe must not wedge supervision
        return "unknown"


_COMPOSER_CHROME = set("›❯┃│─━╭╮╰╯┌┐└┘→>-|+ \t")


def composer_state(runtime, name: str | None) -> str:
    """``empty`` / ``draft`` / ``unknown`` from non-dim composer cells; never edits it."""
    import re

    from corral import embed
    from corral.textutil import text_width

    styled = embed.capture(name, 0, 0) if name else None
    if not styled:
        return "unknown"
    ansi = re.compile(r"\x1b\[[0-9;:?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
    rows = []
    for line in styled.splitlines():
        width = max(1, text_width(ansi.sub("", line)))
        cells = embed.parse_screen(line, width, 1)[0]
        rows.append("".join(" " if cell.dim else (cell.ch or "") for cell in cells))
    region = runtime.composer_region("\n".join(rows))
    if region is None:
        return "unknown"
    if runtime.id == "cursor":
        region = region.splitlines()[0] if region else ""
    content = "".join(ch for ch in region if ch not in _COMPOSER_CHROME)
    for placeholder in _PLACEHOLDERS:
        content = content.replace(placeholder.replace(" ", ""), "")
    return "draft" if any(ch.isalnum() for ch in content) else "empty"


# Idle composer hints some runtimes draw without the dim attribute.
_PLACEHOLDERS = ("Ask anything", "Add a follow-up", "Plan, search, build anything")


# Startup workspace-trust dialogs of freshly launched agents: the caller chose
# this directory for this run, and Corral's product default skips permission
# prompts. Only the exact affirmative option is ever confirmed.
_TRUST_OPTIONS = {"claude": "Yes, I trust this folder"}
_STARTUP_WAIT = 45.0


def _plain(name: str) -> str:
    import re

    from corral import embed

    raw = embed.capture(name, 0, 0) or ""
    return re.sub(r"\x1b\[[0-9;:?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", raw)


def accept_startup_trust(runtime_id: str, name: str) -> bool:
    """Confirm a fresh pane's workspace-trust dialog; True when one was accepted."""
    from corral import embed

    option = _TRUST_OPTIONS.get(runtime_id)
    deadline = time.monotonic() + _STARTUP_WAIT
    while option and time.monotonic() < deadline:
        screen = _plain(name)
        if option not in screen:
            if "enter to confirm" not in screen.lower() and screen.strip():
                return False  # no dialog: the composer is up
            time.sleep(0.5)
            continue
        for _ in range(4):
            selected = [line for line in screen.splitlines() if "❯" in line]
            if selected and option in selected[-1]:
                embed.send_key(name, "Enter")
                time.sleep(1.0)
                return option not in _plain(name)
            embed.send_key(name, "Down")
            time.sleep(0.3)
            screen = _plain(name)
        return False
    return False


class Supervisor:
    def __init__(self, store: RunStore, run_id: str, hub):
        self.store, self.run_id, self.hub = store, run_id, hub
        record = store.get(run_id, private=True)
        self.spec = record["spec"]
        self.runtime = hub.registry.get(self.spec["runtime"])
        self.key: str | None = record.get("session_ref")
        self.session: dict = {}
        self._beat = 0.0

    # -- small helpers -----------------------------------------------------

    def record(self) -> dict:
        return self.store.get(self.run_id)

    def update(self, **changes) -> dict:
        return self.store.update(self.run_id, changes)

    def heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._beat >= _HEARTBEAT:
            self._beat = now
            self.store.heartbeat(self.run_id)

    def fail(self, delivery: str, code: str, message: str) -> None:
        execution = "failed" if delivery == "rejected" else "unknown"
        self.update(delivery=delivery, execution=execution, error={"code": code, "message": message})

    def host_name(self) -> str | None:
        return (self.session or {}).get("keepalive_name") or self.record().get("host")

    def pane_dead(self) -> bool:
        from corral import embed

        name = self.host_name()
        return bool(name) and embed.pane_liveness(name) == "dead"

    # -- admission -----------------------------------------------------------

    def admit_existing(self) -> None:
        session = resolve_target(self.hub.registry, self.spec["session_ref"])
        if str(session.get("cwd") or "") != self.spec["cwd"] or str(session.get("path") or "") != \
                self.spec["history_path"]:
            raise ControlError("target_changed", "The session's project or history changed after submission.")
        if session.get("live") and not session.get("keepalive_name"):
            raise ControlError("external_session", "A process outside Corral now owns this session.")
        self._seed(session)
        self.key = self.spec["session_ref"]
        while True:
            phase = _phase(session) if session.get("live") else "idle"
            if phase == "waiting":
                raise ControlError("target_waiting_input",
                                   "The session is waiting on an earlier question; answer it before delegating.")
            if phase != "working":
                return
            if self.spec["on_busy"] == "reject":
                raise ControlError("target_busy", "The session is executing another turn.")
            if self.spec["on_busy"] == "steer":
                return
            if expired(self.spec, self.record()):
                raise ControlError("queue_expired", "The target stayed busy past --queue-timeout; nothing was sent.")
            self.update(execution="queued", queue_reason="target_busy")
            self.heartbeat()
            self._process_messages(None)
            if self.record()["execution"] in SETTLED:
                return
            time.sleep(_BUSY_POLL)
            session = self.runtime.refresh_session(session) or session
            from corral import liveness

            liveness.annotate([session])

    def admit_fresh(self) -> None:
        import uuid

        ident = self.run_id.removeprefix("run_")[:8]
        plan = fresh_plan(self.runtime, self.spec["cwd"], self.spec.get("model"))
        native_id = self.record().get("native_id")
        if self.runtime.id == "pi":
            native_id = ident  # Pi pins --session-id to the hosted ident
        elif native_id is None and self.runtime.session_id_flag:
            native_id = str(uuid.uuid4())
        if native_id and self.runtime.id != "pi":
            plan = self.runtime.with_session_id(plan, native_id) or plan
        self.update(execution="starting", host_ident=ident, native_id=native_id)
        title = " ".join(self.spec["prompt"].split())[:48] or "Delegated task"
        payload = self.hub.host_planned_session(plan, self.runtime.id, title, self.spec["cwd"], ident=ident)
        self.key = payload["key"]
        self.session = self.hub.require_session(self.key)
        name = self.session.get("keepalive_name")
        self.update(host=name)
        if name and accept_startup_trust(self.runtime.id, name):
            self.update(startup_dialog="workspace_trust_accepted")

    def _seed(self, session: dict) -> None:
        """Give this process's hub the exact session without a full rescan."""
        store = self.hub.store
        runtime_id = str(session["source"])
        with store.lock:
            bucket = [s for s in store.sessions.get(runtime_id, []) if s.get("id") != session.get("id")]
            store.sessions[runtime_id] = [session, *bucket]
            if session.get("keepalive_name"):
                from corral.models import session_key

                store.hosted[session_key(session)] = session["keepalive_name"]
        self.session = session

    def dispatch(self) -> int:
        """Deliver the initial brief once. Returns the SessKit baseline seq."""
        import sesskit

        from corral import embed
        from corral.remote.sessions import ActionError, PartialInjectionError

        self.update(execution="starting")
        deadline = time.monotonic() + _DISPATCH_TIMEOUT
        try:
            baseline = self.hub.prepare_text_delivery(self.key, deadline)
        except ActionError as exc:
            raise ControlError("target_unavailable", exc.message) from exc
        self.session = self.hub.require_session(self.key)
        name = self.session.get("keepalive_name")
        if self.record().get("target_control") == "hosted":
            # Only a pane that existed before this run can hold residue or a human draft.
            check = composer_state(self.runtime, name)
            self.update(composer_check=check)
            if check == "draft":
                raise ControlError("composer_not_empty",
                                   "The session's input box already holds text; it was left untouched.")
        base_seq = -1
        if self.session.get("path"):
            snapshot = sesskit.load_activity(self.session)
            base_seq = max((e.seq for e in snapshot.events), default=-1)
        self.update(host=name, baseline_seq=base_seq, delivery="dispatching", execution="running")
        text = wire_prompt(self.run_id, "initial", self.spec["prompt"])
        try:
            with embed.input_transaction(name, require_process_lock=True):
                self.hub.send_text(self.key, text)
            self.hub.verify_text_injected(self.key, baseline)
        except ActionError as exc:
            # Certain rejection: nothing reached the composer.
            self.fail("rejected", "input_rejected", exc.message)
            return base_seq
        except PartialInjectionError as exc:
            self.update(delivery="unknown", error={"code": "delivery_uncertain", "message": exc.message})
            return base_seq
        self.update(delivery="input_submitted")
        return base_seq

    # -- follow-up messages ----------------------------------------------------

    def _process_messages(self, observation: dict | None) -> None:
        from corral import embed
        from corral.remote.sessions import ActionError, PartialInjectionError

        for item in self.store.pending_messages(self.run_id):
            body, ident = item["body"], item["id"]
            record = self.record()
            dispatched = record["delivery"] not in {"accepted"}
            try:
                if body["action"] == "interrupt":
                    if not dispatched:
                        self.store.message_state(ident, "applied", "Run cancelled before any input was sent.")
                        self.update(delivery="rejected", execution="interrupted",
                                    error={"code": "cancelled", "message": "Interrupted before dispatch."})
                        return
                    if observation and observation.get("attribution") == "mixed":
                        self.store.message_state(ident, "rejected", "Other input shares this turn; not interrupting.")
                        continue
                    self.hub.interrupt_session(self.key)
                    self.store.message_state(ident, "input_submitted")
                elif not dispatched:
                    self.store.message_state(ident, "queued", "Waiting for the initial brief to be delivered.")
                    continue
                elif body["action"] == "answer":
                    outcome = self.hub.answer_question(self.key, body["request_id"], body["answers"])
                    status = outcome.get("status")
                    self.store.message_state(ident, "input_submitted" if status == "delivered" else "rejected",
                                             outcome.get("detail"))
                else:
                    name = self.host_name()
                    with embed.input_transaction(name, require_process_lock=True):
                        self.hub.send_text(self.key, wire_prompt(self.run_id, ident, body["prompt"]))
                    self.store.message_state(ident, "input_submitted")
            except ActionError as exc:
                self.store.message_state(ident, "rejected", exc.message)
            except PartialInjectionError as exc:
                self.store.message_state(ident, "unknown", exc.message)

    # -- observation -------------------------------------------------------------

    def _bind_native(self) -> bool:
        """A fresh session gets its native history only after its first turn.

        Preassigned ids (Claude ``--session-id``, Pi ident) bind exactly; the
        others use the store's hosted-identity machinery (Codex claims).
        """
        native_id = self.record().get("native_id")
        if native_id:
            found = [s for s in self.runtime.scan_sessions(20, keep_ids={native_id}) if s.get("id") == native_id]
            if not found or not found[0].get("path"):
                return False
            from corral.models import session_key

            session = found[0]
            session["keepalive_name"] = self.host_name()
            self._seed(session)
            self.key = session_key(session)
            self.update(session_ref=self.key, history_path=str(session["path"]), identity_evidence="preassigned")
            return True
        self.hub.store.refresh()
        key = self.hub.resolve_session_key(self.key)
        session = self.hub.store.find_session(key)
        if not session or session.get("provisional") or not session.get("path"):
            return False
        self.key, self.session = key, session
        self.update(session_ref=key, history_path=str(session["path"]), identity_evidence="hosted_binding")
        return True

    def _follow_continuation(self) -> bool:
        """Resume may continue in a new native history (Claude ``superseded_by``).

        Only the exact original id or a session whose ``continued_from`` chain
        names it qualifies; the run marker then proves which file holds the run.
        """
        from corral.models import session_key

        runtime_id, _, original = str(self.spec.get("session_ref") or self.key or "").partition(":")
        if not original or runtime_id != self.runtime.id:
            return False
        sessions = self.runtime.scan_sessions(50, keep_ids={original})
        chain = [s for s in sessions if s.get("id") == original or original in (s.get("continued_from") or [])]
        if not chain:
            return False
        latest = max(chain, key=lambda s: float(s.get("mtime") or 0))
        if str(latest.get("path") or "") in {"", str(self.session.get("path") or "")}:
            return False
        latest["keepalive_name"] = self.host_name()
        self._seed(latest)
        self.key = session_key(latest)
        self.update(session_ref=self.key, history_path=str(latest["path"]))
        return True

    def _bind_target_lock(self) -> None:
        from contextlib import ExitStack

        if getattr(self, "_locks", None) is None:
            self._locks = ExitStack()
        if self.key:
            self._locks.enter_context(file_lock(self.store, "target:" + self.key))

    def observe(self, base_seq: int) -> None:
        import sesskit
        from sesskit.models import ActivitySnapshot

        reader = None
        events: dict[int, object] = {}
        published = base_seq
        last_bind = 0.0
        started = time.monotonic()
        last_follow = started
        quiet_since = time.monotonic()
        screen, screen_since = None, time.monotonic()
        while True:
            self.heartbeat()
            record = self.record()
            if record["execution"] in SETTLED:
                return
            if not self.session.get("path") or self.session.get("provisional"):
                self._process_messages(None)
                if self.pane_dead():
                    self.fail("unknown" if record["delivery"] != "rejected" else "rejected", "terminal_exited",
                              "The hosted terminal exited before native history appeared.")
                    return
                if time.monotonic() - last_bind >= _BIND_REFRESH:
                    last_bind = time.monotonic()
                    if self._bind_native():
                        self._bind_target_lock()
                time.sleep(_POLL)
                continue
            if reader is None:
                reader = sesskit.open_activity_reader(self.session)
            poll = reader.poll()
            if poll.reset and events:
                self.fail("unknown", "history_reset", "The native history was replaced; attribution is unknown.")
                return
            for event in poll.events:
                events[event.seq] = event
            if poll.events:
                quiet_since = time.monotonic()
            ordered = [events[k] for k in sorted(events)]
            snapshot = ActivitySnapshot(state=poll.state, events=tuple(ordered), outcome=poll.outcome,
                                        cursor=poll.cursor, generation=poll.generation)
            observation = correlate(ordered, list(sesskit.derive_turns(snapshot, runtime=self.runtime.id)),
                                    self.run_id, base_seq)
            self._process_messages(observation)
            if self.record()["execution"] in SETTLED:
                return
            dead = self.pane_dead()
            if not observation["recorded"]:
                if dead:
                    self.fail("unknown", "terminal_exited", "The terminal exited before the brief was recorded.")
                    return
                now = time.monotonic()
                if self.spec["mode"] == "submit" and now - started > 4.0 and now - last_follow >= _BIND_REFRESH:
                    last_follow = now
                    if self._follow_continuation():
                        reader, events, published = None, {}, -1
                        base_seq = -1  # the run id in the marker is unique; a new file has no older copy
                time.sleep(_POLL)
                continue
            for event in observation["events"]:
                if event.seq > published:
                    self.store.append(self.run_id, f"activity:{poll.generation}:{event.seq}",
                                      {"kind": "activity", **compact_event(event)})
                    published = event.seq
            current = _screen_hash(self.host_name())
            if current != screen:
                screen, screen_since = current, time.monotonic()
            changes = self._observation_changes(record, observation, dead,
                                                min(time.monotonic() - quiet_since,
                                                    time.monotonic() - screen_since))
            if changes:
                self.update(**changes)
            if changes.get("execution") in SETTLED:
                return
            time.sleep(_POLL)

    def _observation_changes(self, record: dict, observation: dict, dead: bool, quiet: float) -> dict:
        changes: dict = {"delivery": "native_recorded", "turn_id": observation["turn_id"],
                         "attribution": observation["attribution"], "error": None}
        outcome = observation["outcome"]
        phase = _phase(self.session) if not dead else "idle"
        waiting = observation["pending_interaction"] or phase == "waiting" or outcome == "awaiting_input"
        if waiting:
            changes["execution"] = "waiting_input"
            changes["questions"] = self._questions()
        else:
            changes["execution"] = "running"
            changes["questions"] = []
        final = _FINAL.get(outcome)
        if final and not waiting:
            last = observation["events"][-1] if observation["events"] else None
            settled = observation["outcome_evidence"] == "native" or dead or (
                quiet >= _SETTLE_QUIET and phase not in {"working", "waiting"}
                and (final != "completed" or (last is not None and last.type == "assistant_message"))
            )
            if settled:
                result = build_result(observation)
                observed = (result["observed_models"] or [None])[-1]
                result["finality"] = "native" if observation["outcome_evidence"] == "native" else "inferred"
                changes.update(execution=final, result=result, observed_model=observed,
                               finality=result["finality"],
                               model_check=model_check(self.spec.get("model"), result["observed_models"]))
                if observation.get("outcome_error"):
                    changes["error"] = {"code": "runtime_error", "message": observation["outcome_error"]}
                return changes
        if dead:
            changes.update(execution="unknown", error={"code": "terminal_exited",
                                                       "message": "The terminal exited before the turn settled."})
        return changes

    def _questions(self) -> list[dict]:
        try:
            prompts = self.hub.prompts(self.key)
        except Exception:  # noqa: BLE001 - questions are evidence, never a crash
            return []
        keep = ("request_id", "question_id", "question", "header", "options", "option_details",
                "multi_select", "allow_custom", "custom_needs_choice")
        return [{k: p.get(k) for k in keep if k in p} for p in prompts]

    # -- entry -----------------------------------------------------------------------

    def run(self) -> None:
        record = self.record()
        if record["delivery"] == "accepted":
            if self.spec["mode"] == "create":
                self.admit_fresh()
            else:
                self.admit_existing()
            if self.record()["execution"] in SETTLED:
                return
            base_seq = self.dispatch()
        else:
            # Recovery never replays a brief that may already have been typed.
            if record["delivery"] == "dispatching":
                self.update(delivery="unknown", error={"code": "delivery_uncertain",
                                                       "message": "The supervisor stopped while typing the brief."})
            base_seq = int(record.get("baseline_seq", -1))
            if not self.key and record.get("host_ident"):
                self.key = f"{self.runtime.id}:{record['host_ident']}"
            self._bind_native()
            self.session = self.hub.store.find_session(self.hub.resolve_session_key(self.key or "")) or {}
        if self.record()["execution"] in SETTLED:
            return
        self.observe(base_seq)


def _settle_crash(store: RunStore, run_id: str, exc: BaseException) -> None:
    current = store.get(run_id)
    if current["execution"] in SETTLED:
        return
    code = getattr(exc, "code", "supervisor_error")
    message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
    before_input = current["delivery"] == "accepted"
    store.update(run_id, {"delivery": "rejected" if before_input else current["delivery"],
                          "execution": "failed" if before_input else "unknown",
                          "error": {"code": code, "message": message}})


def expired(spec: dict, record: dict) -> bool:
    limit = int(spec.get("queue_timeout") or 0)
    return bool(limit) and record.get("delivery") == "accepted" and time.time() - record["created_at"] > limit


def model_check(requested: str | None, observed: list[str]) -> str | None:
    if not requested:
        return None
    if not observed:
        return "unobserved"
    wanted = requested.lower().split("/")[-1].split(":")[0]
    if all(m.lower().split("/")[-1] == wanted for m in observed):
        return "match"
    if all(wanted in m.lower() for m in observed):
        return "alias_match"
    return "mismatch"


def _earlier_runs(store: RunStore, run_id: str, target: str) -> bool:
    """Runs on one session dispatch in submit order: wait while an older one is unsettled."""
    if target.startswith("run:"):
        return False
    pending = store.for_target(target)
    return bool(pending) and pending[0]["run_id"] != run_id


def main(run_id: str) -> int:
    import corral

    # Import everything up front: a package upgrade during a long run must not
    # mix module versions inside this process.
    from corral import attention_signals, embed, liveness  # noqa: F401
    from corral.remote.sessions import SessionHub

    store = RunStore()
    with file_lock(store, "run:" + run_id) as owned:
        if not owned:
            return 0
        store.heartbeat(run_id, started=True)
        record = store.get(run_id, private=True)
        if record["execution"] in SETTLED:
            return 0
        store.update(run_id, {"supervisor_version": corral.__version__})
        spec = record["spec"]
        target = spec.get("session_ref") or ("run:" + run_id)
        try:
            while True:
                if not _earlier_runs(store, run_id, target):
                    with file_lock(store, "target:" + target) as owns_target:
                        if owns_target:
                            Supervisor(store, run_id, SessionHub()).run()
                            return 0
                store.heartbeat(run_id)
                current = store.get(run_id)
                if current["execution"] in SETTLED:
                    return 0
                if expired(spec, current):
                    raise ControlError("queue_expired", "An earlier run kept the session past --queue-timeout.")
                store.update(run_id, {"queue_reason": "earlier_run_on_session"})
                time.sleep(_BUSY_POLL)
        except Exception as exc:  # noqa: BLE001 - every exit leaves an honest terminal state
            _settle_crash(store, run_id, exc)
            return 1


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].startswith("run_"):
        raise SystemExit(2)
    os.environ.setdefault("CORRAL_CONTROL_SUPERVISOR", "1")
    raise SystemExit(main(sys.argv[1]))
