"""Delegated per-run supervision over Corral's hosted terminals.

One process per run. The execution engine is the shared
:func:`agent_session_control.supervisor.run_supervisor`
(:class:`SupervisorRunner`); Corral supplies explicit host hooks built by
:func:`corral_hooks` from its own terminal/observer/identity machinery:

- terminals: ``SessionHub`` hosting, readiness, verified input, interrupt,
  question answers (``corral.remote.sessions`` + ``corral.embed``);
- observation: SessKit incremental readers + shared correlation with
  Corral's legacy marker namespace, native rebind/continuation, Corral
  attention phases and pending-question evidence;
- identity: managed Pi/Codex preparation, preassigned native ids, hosted
  binding (never newest-file/cwd guessing).

The runner owns run/target locks, admission queueing, exactly-once dispatch,
follow-up messages and settlement. Hook closures hold Corral's store handle
only to preserve Corral-exact terminal evidence (codes, messages and
delivery values) at binding/death/timeout edges the generic loop would
otherwise coarsen; every such write mirrors the historical outcome byte for
byte and yields control back to the shared loop.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from contextlib import ExitStack, contextmanager

from agent_session_control.supervisor import (
    CertainFailure,
    SupervisorHooks,
    SupervisorRunner,
    TargetError,
    UncertainDelivery,
    run_supervisor,
)
from agent_session_control.supervisor import (
    file_lock as _shared_file_lock,
)
from agent_session_control.supervisor import (
    lock_held as _shared_lock_held,
)

from corral.control import fresh_plan
from corral.control_store import SETTLED, ControlError, RunStore

_BUSY_POLL = 3.0
_BIND_REFRESH = 3.0
# Corral-exact no-usable-history budget (default 150s). The shared runner
# receives a strictly larger budget (below) so the host hook below always
# settles first with Corral's exact evidence; the runner budget remains as a
# backstop. Tests shrink the budget through CORRAL_HISTORY_WAIT_TIMEOUT.
_HISTORY_WAIT_TIMEOUT = 150.0
_RUNNER_HISTORY_WAIT_MARGIN = _BIND_REFRESH


def _history_wait_timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("CORRAL_HISTORY_WAIT_TIMEOUT") or _HISTORY_WAIT_TIMEOUT))
    except ValueError:
        return _HISTORY_WAIT_TIMEOUT


def _runner_history_wait() -> float:
    return _history_wait_timeout() + _RUNNER_HISTORY_WAIT_MARGIN


# Evidence-specific wording for the bounded no-usable-history outcome; the run
# itself stays recoverable and its delivery/host/session ownership untouched.
_HISTORY_WAIT_ERRORS = {
    "native_history_missing": (
        "No native history appeared within {seconds}s of the submitted brief. Its outcome is unknown "
        "and stays recoverable; the terminal was not judged dead and the brief was not resent."
    ),
    "native_history_unavailable": (
        "The session's native history stayed unreadable for {seconds}s. Its outcome is unknown and "
        "stays recoverable; nothing was resent or interrupted."
    ),
}


def _history_wait_error(code: str) -> dict:
    """Persisted evidence for the bounded no-usable-history outcome."""
    return {"code": code, "message": _HISTORY_WAIT_ERRORS[code].format(seconds=int(_history_wait_timeout()))}


@contextmanager
def file_lock(store: RunStore, identity: str, *, blocking: bool = False):
    """Cross-process lock shared with the runner (identical lock files)."""
    store.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _shared_file_lock(str(store.root), identity, blocking=blocking) as owned:
        yield owned


def lock_held(store: RunStore, identity: str) -> bool:
    return _shared_lock_held(str(store.root), identity)


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
_LOGIN_PROMPT = re.compile(r"(?i)\b(?:press any key to log ?in|please log ?in|not logged in|sign in to)\b")
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


def _settle_crash(store: RunStore, run_id: str, exc: BaseException) -> None:
    current = store.get(run_id)
    if current["execution"] in SETTLED:
        return
    code = getattr(exc, "code", "supervisor_error")
    message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
    before_input = current["delivery"] == "accepted"
    store.update(
        run_id,
        {
            "delivery": "rejected" if before_input else current["delivery"],
            "execution": "failed" if before_input else "unknown",
            "error": {"code": code, "message": message},
        },
    )


def _seed_hub(hub, session: dict) -> None:
    """Give this process's hub the exact session without a full rescan."""
    store = hub.store
    runtime_id = str(session["source"])
    with store.lock:
        bucket = [s for s in store.sessions.get(runtime_id, []) if s.get("id") != session.get("id")]
        store.sessions[runtime_id] = [session, *bucket]
        if session.get("keepalive_name"):
            from corral.models import session_key

            store.hosted[session_key(session)] = session["keepalive_name"]


def _settle_unknown(store: RunStore, run_id: str, code: str, message: str) -> None:
    """Corral-exact recoverable-unknown evidence, then yield to the shared loop.

    The shared loop exits on its next settled-state read without overwriting
    this evidence (it only writes on timeout/death transitions, neither of
    which fires before that read). Delivery is preserved, never reset.
    """
    try:
        store.update(run_id, {"execution": "unknown", "error": {"code": code, "message": message}})
    except ControlError:
        pass  # someone settled first; our evidence is redundant


def _lock_target(state: dict, store: RunStore, key: str) -> bool:
    """Hold the cross-process target lock for a rebound native key."""
    if not key or key in state["locked_targets"]:
        return True
    owned = state["locks"].enter_context(_shared_file_lock(str(store.root), "target:" + key))
    if not owned:
        return False
    state["locked_targets"].add(key)
    return True


def _bind_native(state: dict, store: RunStore, run_id: str, hub, runtime) -> bool | str:
    """Bind a fresh session to its native history; ``"locked"`` on contention.

    A fresh session gets its native history only after its first turn.
    Preassigned ids (Claude ``--session-id``, Pi ident) bind exactly; the
    others use the store's hosted-identity machinery (Codex claims).
    Returns True when bound (store session_ref/history_path updated).
    """
    from corral.models import session_key

    record = store.get(run_id)
    binding = state["binding"]
    native_id = record.get("native_id")
    if native_id:
        found = [s for s in runtime.scan_sessions(20, keep_ids={native_id}) if s.get("id") == native_id]
        if not found or not found[0].get("path"):
            return False
        session = found[0]
        session["keepalive_name"] = binding.get("pane") or record.get("host")
        _seed_hub(hub, session)
        key = session_key(session)
        if not _lock_target(state, store, key):
            return "locked"
        binding.update(session_ref=key, pane=session["keepalive_name"], history_path=str(session["path"]))
        store.update(
            run_id, {"session_ref": key, "history_path": str(session["path"]), "identity_evidence": "preassigned"}
        )
        return True
    hub.store.refresh()
    key = hub.resolve_session_key(binding.get("session_ref") or "")
    session = hub.store.find_session(key)
    if not session or session.get("provisional") or not session.get("path"):
        return False
    if not _lock_target(state, store, key):
        return "locked"
    binding.update(session_ref=key, pane=session.get("keepalive_name"), history_path=str(session["path"]))
    store.update(
        run_id, {"session_ref": key, "history_path": str(session["path"]), "identity_evidence": "hosted_binding"}
    )
    return True


def _follow_continuation(state: dict, store: RunStore, run_id: str, hub, runtime, spec: dict) -> bool | str:
    """Resume may continue in a new native history (Claude ``superseded_by``).

    Only the exact original id or a session whose ``continued_from`` chain
    names it qualifies; the run marker then proves which file holds the run.
    """
    from corral.models import session_key

    binding = state["binding"]
    runtime_id, _, original = str(spec.get("session_ref") or binding.get("session_ref") or "").partition(":")
    if not original or runtime_id != runtime.id:
        return False
    sessions = runtime.scan_sessions(50, keep_ids={original})
    chain = [s for s in sessions if s.get("id") == original or original in (s.get("continued_from") or [])]
    if not chain:
        return False
    latest = max(chain, key=lambda s: float(s.get("mtime") or 0))
    if str(latest.get("path") or "") in {"", str(binding.get("history_path") or "")}:
        return False
    latest["keepalive_name"] = binding.get("pane")
    _seed_hub(hub, latest)
    key = session_key(latest)
    if not _lock_target(state, store, key):
        return "locked"
    binding.update(session_ref=key, history_path=str(latest["path"]))
    store.update(run_id, {"session_ref": key, "history_path": str(latest["path"])})
    obs = state["obs"]
    obs["reader"], obs["events"], obs["base"] = None, {}, -1
    return True


def _hub_session(hub, key: str) -> dict:
    try:
        return hub.store.find_session(key) or {}
    except Exception:  # noqa: BLE001 - store probes must not wedge supervision
        return {}


def _pane_of(state: dict, record_host: str | None = None) -> str | None:
    return state["binding"].get("pane") or record_host


def corral_hooks(store: RunStore, run_id: str, hub) -> tuple[SupervisorHooks, dict]:
    """Explicit Corral host hooks for the shared runner plus their mutable state.

    ``hub`` is Corral's ``SessionHub`` (hosted terminals). Every side effect
    stays in Corral-owned machinery; the shared runner only orchestrates.
    """
    record = store.get(run_id, private=True)
    spec = record["spec"]
    runtime = hub.registry.get(spec["runtime"])
    state: dict = {
        "binding": {},
        "locks": ExitStack(),
        "locked_targets": {spec.get("session_ref")},
        "startup_dialog": None,
        "obs": {
            "reader": None,
            "events": {},
            "base": None,
            "missing_since": None,
            "unavailable_since": None,
            "last_bind": 0.0,
            "last_follow": time.monotonic(),
            "started": time.monotonic(),
        },
        # Only a pane that existed before this run can hold residue or a human draft.
        "composer_guard": spec.get("mode") != "create" and record.get("target_control") == "hosted",
    }

    def resolve_target(session_ref: str) -> dict:
        from corral.control import resolve_target as _host_resolve

        try:
            session = _host_resolve(hub.registry, session_ref)
        except ControlError as exc:
            raise TargetError(exc.code, exc.message) from exc
        _seed_hub(hub, session)
        mapping = dict(session)
        mapping["pane"] = session.get("keepalive_name")
        state["binding"] = {"session_ref": session_ref, "pane": mapping["pane"], "history_path": session.get("path")}
        return mapping

    def target_busy(session: dict) -> bool:
        return bool(session.get("live")) and _phase(session) == "working"

    def target_waiting(session: dict) -> bool:
        return _phase(session) == "waiting"

    def host_fresh(fresh_spec: dict, ident: str) -> dict:
        import uuid

        plan = fresh_plan(runtime, fresh_spec["cwd"], fresh_spec.get("model"))
        stored = store.get(run_id, private=True)
        native_id = stored.get("native_id")
        if runtime.id == "pi":
            native_id = ident  # Pi pins --session-id to the hosted ident
        elif native_id is None and runtime.session_id_flag:
            native_id = str(uuid.uuid4())
        if native_id and runtime.id != "pi":
            plan = runtime.with_session_id(plan, native_id) or plan
        title = " ".join(fresh_spec["prompt"].split())[:48] or "Delegated task"
        payload = hub.host_planned_session(plan, runtime.id, title, fresh_spec["cwd"], ident=ident)
        key = payload["key"]
        session = hub.require_session(key)
        name = session.get("keepalive_name")
        store.update(run_id, {"host": name})
        if name and accept_startup_trust(runtime.id, name):
            state["startup_dialog"] = "workspace_trust_accepted"
        state["binding"] = {"session_ref": key, "pane": name, "native_id": native_id, "history_path": None}
        return dict(state["binding"])

    def prepare(session_ref: str, deadline: float):
        from corral.remote.sessions import ActionError

        key = state["binding"].get("session_ref") or session_ref
        try:
            baseline = hub.prepare_text_delivery(key, deadline)
        except ActionError as exc:
            name = (_hub_session(hub, key)).get("keepalive_name")
            if name and _LOGIN_PROMPT.search(_plain(name)):
                raise CertainFailure(
                    "runtime_login_required",
                    f"{runtime.display_name} is not logged in on this machine; "
                    "log in once in its own terminal, then submit again.",
                ) from exc
            raise CertainFailure("target_unavailable", exc.message) from exc
        session = hub.require_session(key)
        name = session.get("keepalive_name")
        if name:
            store.update(run_id, {"host": name})
            state["binding"]["pane"] = name
        if state["composer_guard"]:
            check = composer_state(runtime, name)
            store.update(run_id, {"composer_check": check})
            if check == "draft":
                raise CertainFailure(
                    "composer_not_empty", "The session's input box already holds text; it was left untouched."
                )
        return baseline

    def _send_text(key: str, text: str) -> None:
        from corral import embed
        from corral.remote.sessions import ActionError, PartialInjectionError

        name = _pane_of(state)
        try:
            with embed.input_transaction(name, require_process_lock=True):
                hub.send_text(key, text)
        except ActionError as exc:
            raise CertainFailure("input_rejected", exc.message) from exc
        except PartialInjectionError as exc:
            raise UncertainDelivery(exc.message) from exc

    def submit(session_ref: str, text: str) -> None:
        _send_text(state["binding"].get("session_ref") or session_ref, text)

    def verify(session_ref: str, baseline) -> None:
        # Uncaught hub errors settle exactly like the historical supervisor:
        # delivery preserved, execution unknown, hub error code kept.
        hub.verify_text_injected(state["binding"].get("session_ref") or session_ref, baseline)

    def interrupt(session_ref: str) -> None:
        from corral.remote.sessions import ActionError, PartialInjectionError

        key = state["binding"].get("session_ref") or session_ref
        profile_keys = tuple(getattr(runtime, "interrupt_keys", ("Escape",)) or ("Escape",))
        _ = profile_keys  # interrupt keys stay adapter-owned; the hub sends them.
        try:
            hub.interrupt_session(key)
        except ActionError as exc:
            raise CertainFailure(exc.code or "interrupt_failed", exc.message) from exc
        except PartialInjectionError as exc:
            raise UncertainDelivery(exc.message) from exc

    def answer(session_ref: str, request_id: str, answers) -> dict:
        outcome = hub.answer_question(state["binding"].get("session_ref") or session_ref, request_id, answers)
        return {"status": (outcome or {}).get("status"), "detail": (outcome or {}).get("detail")}

    def steer(session_ref: str, wire_text: str) -> None:
        _send_text(state["binding"].get("session_ref") or session_ref, wire_text)

    def _lookup_session() -> dict:
        binding = state["binding"]
        return _hub_session(hub, binding.get("session_ref") or spec.get("session_ref") or "")

    def _ensure_reader(session: dict):
        import sesskit

        obs = state["obs"]
        if obs["reader"] is None:
            obs["reader"] = sesskit.open_activity_reader(dict(session))
        return obs["reader"]

    def open_observation(binding: dict):
        state["obs"] = {
            "reader": None,
            "events": {},
            "base": None,
            "missing_since": None,
            "unavailable_since": None,
            "last_bind": 0.0,
            "last_follow": time.monotonic(),
            "started": time.monotonic(),
        }
        return "corral-obs"

    def poll_observation(handle: str, poll_run_id: str, base_seq: int):
        import sesskit
        from agent_session_control.correlation import correlate as _correlate
        from sesskit.models import ActivitySnapshot

        obs = state["obs"]
        if obs["base"] is None:
            obs["base"] = base_seq
        session = _lookup_session()
        if not session.get("path") or session.get("provisional"):
            now = time.monotonic()
            if now - obs["last_bind"] >= _BIND_REFRESH:
                obs["last_bind"] = now
                bound = _bind_native(state, store, run_id, hub, runtime)
                if bound == "locked":
                    _settle_unknown(
                        store, run_id, "target_locked", "Another supervisor owns this native session; control stopped."
                    )
                    return ({"recorded": False}, False, "ready")
                if bound:
                    session = _lookup_session()
                    obs["missing_since"] = None
            if not session.get("path") or session.get("provisional"):
                if _pane_dead(state, store, run_id):
                    _settle_unknown(
                        store, run_id, "terminal_exited", "The hosted terminal exited before native history appeared."
                    )
                    return ({"recorded": False}, False, "ready")
                if obs["missing_since"] is None:
                    obs["missing_since"] = now
                if now - obs["missing_since"] >= _history_wait_timeout():
                    error = _history_wait_error("native_history_missing")
                    _settle_unknown(store, run_id, error["code"], error["message"])
                    return ({"recorded": False}, False, "ready")
                return ({"recorded": False}, False, "unavailable")
            obs["missing_since"] = None
        try:
            reader = _ensure_reader(session)
            poll = reader.poll()
        except Exception:
            obs["unavailable_since"] = obs["unavailable_since"] or time.monotonic()
            if time.monotonic() - obs["unavailable_since"] >= _history_wait_timeout():
                error = _history_wait_error("native_history_unavailable")
                _settle_unknown(store, run_id, error["code"], error["message"])
                return ({"recorded": False}, False, "ready")
            return ({"recorded": False}, False, "unavailable")
        if poll.reset and obs["events"]:
            _settle_unknown(store, run_id, "history_reset", "The native history was replaced; attribution is unknown.")
            return ({"recorded": False}, False, "ready")
        for event in poll.events:
            obs["events"][event.seq] = event
        ordered = [obs["events"][key] for key in sorted(obs["events"])]
        snapshot = ActivitySnapshot(
            state=poll.state,
            events=tuple(ordered),
            outcome=poll.outcome,
            cursor=poll.cursor,
            generation=poll.generation,
        )
        observation = _correlate(
            ordered,
            list(sesskit.derive_turns(snapshot, runtime=runtime.id)),
            poll_run_id,
            obs["base"],
            namespace="Corral",
        )
        now = time.monotonic()
        if not observation.get("recorded") and _pane_dead(state, store, run_id):
            _settle_unknown(store, run_id, "terminal_exited", "The terminal exited before the brief was recorded.")
            return ({"recorded": False}, False, "ready")
        if (
            spec.get("mode") == "submit"
            and not observation.get("recorded")
            and now - obs["started"] > 4.0
            and now - obs["last_follow"] >= _BIND_REFRESH
        ):
            obs["last_follow"] = now
            followed = _follow_continuation(state, store, run_id, hub, runtime, spec)
            if followed == "locked":
                _settle_unknown(
                    store, run_id, "target_locked", "Another supervisor owns this native session; control stopped."
                )
            elif followed:
                obs["unavailable_since"] = None
        if not observation.get("recorded") and poll.state == "unavailable" and not obs["events"]:
            if obs["unavailable_since"] is None:
                obs["unavailable_since"] = now
            if now - obs["unavailable_since"] >= _history_wait_timeout():
                error = _history_wait_error("native_history_unavailable")
                _settle_unknown(store, run_id, error["code"], error["message"])
                return ({"recorded": False}, False, "ready")
        elif observation.get("recorded"):
            obs["unavailable_since"] = None
        return (observation, False, "ready")

    def close_observation(handle: str) -> None:
        try:
            reader = state["obs"].get("reader")
            close = getattr(reader, "close", None)
            if callable(close):
                close()
        except Exception:  # noqa: BLE001 - teardown must not fail settlement
            pass
        try:
            state["locks"].close()
        except Exception:  # noqa: BLE001 - teardown must not fail settlement
            pass

    def baseline_of(binding: dict) -> int:
        import sesskit

        session = _lookup_session()
        if not session.get("path"):
            return -1
        try:
            snapshot = sesskit.load_activity(dict(session))
        except Exception:  # noqa: BLE001 - unreadable history means unknown baseline
            return -1
        return max((event.seq for event in snapshot.events), default=-1)

    def bind_native(binding: dict):
        """Shared-port native binding: exact preassigned id or hosted claim.

        Called by the runner's await-binding gate while the run has no native
        history. Returns the updated binding once bound, else None. Bounded
        unknown/death/ownership outcomes settle here with Corral's exact
        evidence (the runner sees the settled state and stops without
        overwriting it); transient absence simply retries on the next gate
        pass. Never guesses from recency, cwd or PID; never parses native
        history (SessKit owns that).
        """
        state["binding"] = dict(binding)
        obs = state["obs"]
        now = time.monotonic()
        if _pane_dead(state, store, run_id):
            _settle_unknown(
                store, run_id, "terminal_exited", "The hosted terminal exited before native history appeared."
            )
            return None
        bound = _bind_native(state, store, run_id, hub, runtime)
        if bound == "locked":
            _settle_unknown(
                store, run_id, "target_locked", "Another supervisor owns this native session; control stopped."
            )
            return None
        if bound:
            obs["missing_since"] = None
            return dict(state["binding"])
        if obs["missing_since"] is None:
            obs["missing_since"] = now
        if now - obs["missing_since"] >= _history_wait_timeout():
            error = _history_wait_error("native_history_missing")
            _settle_unknown(store, run_id, error["code"], error["message"])
            return None
        return None

    def is_alive(session_ref: str) -> str:
        from corral import embed

        name = _pane_of(state, store.get(run_id).get("host"))
        if not name:
            return "unknown"
        try:
            return embed.pane_liveness(name)
        except Exception:  # noqa: BLE001 - a broken probe reports unknown, never dead
            return "unknown"

    def screen_hash(session_ref: str):
        return _screen_hash(_pane_of(state))

    def phase(session_ref: str) -> str:
        session = _lookup_session()
        return _phase(session) if session else "unknown"

    def list_questions(session_ref: str) -> list:
        try:
            prompts = hub.prompts(state["binding"].get("session_ref") or session_ref)
        except Exception:  # noqa: BLE001 - questions are evidence, never a crash
            return []
        keep = (
            "request_id",
            "question_id",
            "prompt",
            "header",
            "options",
            "option_details",
            "multi_select",
            "allow_custom",
            "custom_needs_choice",
        )
        return [{key: item.get(key) for key in keep if key in item} for item in prompts]

    hooks = SupervisorHooks(
        resolve_target=resolve_target,
        target_busy=target_busy,
        target_waiting=target_waiting,
        host_fresh=host_fresh,
        prepare=prepare,
        submit=submit,
        verify=verify,
        interrupt=interrupt,
        answer=answer,
        steer=steer,
        open_observation=open_observation,
        poll_observation=poll_observation,
        close_observation=close_observation,
        baseline_of=baseline_of,
        bind_native=bind_native,
        is_alive=is_alive,
        screen_hash=screen_hash,
        phase=phase,
        list_questions=list_questions,
    )
    return hooks, state


def _pane_dead(state: dict, store: RunStore, run_id: str) -> bool:
    from corral import embed

    name = _pane_of(state, store.get(run_id).get("host"))
    try:
        return bool(name) and embed.pane_liveness(name) == "dead"
    except Exception:  # noqa: BLE001 - a broken probe never reports death
        return False


def finalize_run(store: RunStore, run_id: str, state: dict) -> None:
    """Host-owned settle metadata the generic runner does not compute.

    Model compliance and the startup-dialog receipt are Corral product
    evidence; they are patched once, after the shared runner settles, so the
    executor itself stays generic. No-ops when another writer settled first.
    """
    try:
        record = store.get(run_id, private=True)
    except ControlError:
        return
    if record.get("execution") not in SETTLED or record.get("result") is None:
        return
    changes: dict = {}
    if "model_check" not in record:
        observed = record["result"].get("observed_models") or []
        changes["model_check"] = model_check(record.get("spec", {}).get("model"), observed)
    if state.get("startup_dialog") and "startup_dialog" not in record:
        changes["startup_dialog"] = state["startup_dialog"]
    if not changes:
        return
    try:
        store.update(run_id, changes, allow_settled=True)
    except ControlError:
        pass  # settled concurrently; metadata patch is redundant


class Supervisor:
    """Backward-compatible supervisor: shared runner plus Corral host hooks.

    New code should prefer :func:`run_supervisor` with :func:`corral_hooks`
    directly; this facade preserves the historical constructor for existing
    callers. No execution logic lives here.
    """

    def __init__(self, store: RunStore, run_id: str, hub):
        self.store, self.run_id, self.hub = store, run_id, hub
        record = store.get(run_id, private=True)
        self.spec = record["spec"]
        self.runtime = hub.registry.get(self.spec["runtime"])
        self.hooks, self.hook_state = corral_hooks(store, run_id, hub)

    def record(self) -> dict:
        return self.store.get(self.run_id)

    def run(self) -> int:
        rc = run_supervisor(
            self.store, self.run_id, self.hooks, marker_namespace="Corral", history_wait=_runner_history_wait()
        )
        finalize_run(self.store, self.run_id, self.hook_state)
        return rc


def main(run_id: str) -> int:
    import corral

    # Import everything up front: a package upgrade during a long run must not
    # mix module versions inside this process.
    from corral import attention_signals, embed, liveness  # noqa: F401
    from corral.remote.sessions import SessionHub

    store = RunStore()
    # Phase 1 (singleton-guarded): heartbeat, version stamp, host hooks and
    # the submit-order gate. Cancellation queued before any input settles here
    # without needing target ownership, mirroring the runner's own entry.
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
        hub = SessionHub()
        hooks, state = corral_hooks(store, run_id, hub)
        try:
            if store.get(run_id)["delivery"] == "accepted":
                SupervisorRunner(store, run_id, hooks, marker_namespace="Corral").process_messages(None)
        except Exception:  # noqa: BLE001 - the runner entry re-validates below
            pass
        try:
            # Serialize independent runs per session in submit order; the
            # shared runner re-verifies under its own target lock.
            while True:
                if store.get(run_id)["execution"] in SETTLED:
                    return 0
                if not _earlier_runs(store, run_id, target):
                    break
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
    # Phase 2: the shared entry owns the run lock exclusively from here on.
    # Releasing first is REQUIRED, not optional: the lock is non-reentrant,
    # so calling run_supervisor while holding it returns 0 without executing
    # accepted work. Splitting is race-safe: a competing supervisor that wins
    # the lock executes exactly once (target lock plus settled re-checks), and
    # the loser returns 0 under the pre-existing singleton semantic. There is
    # exactly one execution loop: the shared runner's.
    try:
        rc = run_supervisor(store, run_id, hooks, marker_namespace="Corral", history_wait=_runner_history_wait())
        finalize_run(store, run_id, state)
        return rc
    except Exception as exc:  # noqa: BLE001 - every exit leaves an honest terminal state
        _settle_crash(store, run_id, exc)
        return 1
    finally:
        try:
            state["locks"].close()
        except Exception:  # noqa: BLE001 - teardown must not fail settlement
            pass


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].startswith("run_"):
        raise SystemExit(2)
    os.environ.setdefault("CORRAL_CONTROL_SUPERVISOR", "1")
    raise SystemExit(main(sys.argv[1]))
