"""Corral-owned delegation: exact targets, admission checks and result correlation.

Execution goes through Corral's hosted terminals (``SessionHub``); observation
consumes SessKit's normalized activity. No vendor control API is involved.

Run/message identity and evidence correlation are the shared implementation
(:mod:`agent_session_control.correlation`, selected here with Corral's legacy
``Corral`` marker namespace). What stays Corral-owned: exact target
selection, admission policy, launch arguments, caller identity and the
adapter-backed host profiles consumed through the shared port.
"""

from __future__ import annotations

from pathlib import Path

from agent_session_control import correlation as _correlation
from agent_session_control.correlation import (
    MAX_PROMPT_CHARS,
    build_result,
    compact_event,
)
from agent_session_control.correlation import (
    validate_prompt as _shared_validate_prompt,
)
from agent_session_control.errors import EXIT_NOT_FOUND, EXIT_USAGE, ControlError

from corral.control_store import digest
from corral.models import session_key
from corral.runtime.registry import ACTIVE_RUNTIME_IDS

__all__ = [
    "ACTIVE_RUNTIME_IDS",
    "MARKER_NAMESPACE",
    "MAX_PROMPT_CHARS",
    "build_result",
    "caller_identity",
    "compact_event",
    "control_capability",
    "control_profile",
    "correlate",
    "fresh_plan",
    "host_runtime_profiles",
    "library_create_plan",
    "library_resume_plan",
    "marker",
    "marker_with_namespace",
    "own_message",
    "own_message_with_namespace",
    "resolve_target",
    "self_target",
    "target_payload",
    "validate_directory",
    "validate_prompt",
    "wire_prompt",
    "wire_prompt_with_namespace",
]

#: Legacy wire namespace of the visible run/message envelope. This is Corral's
#: own historical value (predates the library); the library default stays
#: neutral and Corral selects this explicitly at every call site below.
MARKER_NAMESPACE = "Corral"

_RESOLVE_DEPTH = 200


def marker(run_id: str, message_id: str) -> str:
    """Thin facade: shared marker with Corral's legacy namespace."""
    return _correlation.marker(run_id, message_id, namespace=MARKER_NAMESPACE)


def wire_prompt(run_id: str, message_id: str, prompt: str) -> str:
    """Thin facade: shared envelope with Corral's legacy namespace."""
    return _correlation.wire_prompt(run_id, message_id, prompt, namespace=MARKER_NAMESPACE)


def own_message(text: str | None, run_id: str) -> str | None:
    """Thin facade: shared ownership match with Corral's legacy namespace.

    Only a new normalized user event may establish input recording; callers
    enforce the user-message restriction in :func:`correlate`. A non-legacy
    namespace never matches the legacy wire marker.
    """
    return _correlation.own_message(text, run_id, namespace=MARKER_NAMESPACE)


def marker_with_namespace(run_id: str, message_id: str, *, namespace: str = MARKER_NAMESPACE) -> str:
    return _correlation.marker(run_id, message_id, namespace=namespace)


def wire_prompt_with_namespace(run_id: str, message_id: str, prompt: str, *, namespace: str = MARKER_NAMESPACE) -> str:
    return _correlation.wire_prompt(run_id, message_id, prompt, namespace=namespace)


def own_message_with_namespace(text: str | None, run_id: str, *, namespace: str = MARKER_NAMESPACE) -> str | None:
    return _correlation.own_message(text, run_id, namespace=namespace)


def correlate(events: list, turns: list, run_id: str, baseline_seq: int) -> dict:
    """Thin facade: shared correlation with Corral's legacy namespace.

    Inputs from anyone else inside the bound range make attribution ``mixed``.
    Markers quoted in assistant or tool output never count as delivery.
    """
    return _correlation.correlate(events, turns, run_id, baseline_seq, namespace=MARKER_NAMESPACE)


def validate_prompt(text: str) -> str:
    """Thin facade: shared prompt validation (same limits, codes and hint)."""
    return _shared_validate_prompt(text)


def caller_identity() -> dict:
    """The calling agent as Corral knows it; hosted panes export their own identity."""
    import os

    from corral.legacy_names import getenv

    runtime = getenv("RUNTIME") or None
    ident = getenv("SESSION_ID") or None
    actor = os.environ.get("CORRAL_ACTOR") or (f"{runtime}:{ident}" if runtime and ident else "local-user")
    return {"actor": actor, "runtime": runtime, "pane_ident": ident}


# -- targets -------------------------------------------------------------------


def resolve_target(registry, ref: str) -> dict:
    """Resolve one complete ``runtime:id`` without recency guessing."""
    runtime_id, sep, ident = str(ref or "").partition(":")
    if not sep or not ident:
        raise ControlError(
            "usage_error",
            "Control writes need a complete runtime:id reference.",
            EXIT_USAGE,
            hint="Copy runtime and id from search/show output, e.g. claude:<full-session-id>.",
        )
    if runtime_id not in ACTIVE_RUNTIME_IDS:
        raise ControlError(
            "usage_error",
            f"Unsupported runtime: {runtime_id}",
            EXIT_USAGE,
            hint="Supported runtimes: " + ", ".join(sorted(ACTIVE_RUNTIME_IDS)),
        )
    runtime = registry.get(runtime_id)
    sessions = runtime.scan_sessions(_RESOLVE_DEPTH, keep_ids={ident})
    matches = [s for s in sessions if s.get("id") == ident]
    if len(matches) != 1:
        raise ControlError(
            "not_found",
            f"No session has the exact reference {ref}.",
            EXIT_NOT_FOUND,
            hint="Prefixes are not accepted for writes; use the full id from search or show.",
        )
    from corral import liveness

    liveness.annotate(matches)
    return matches[0]


def control_capability(session: dict) -> tuple[str, str]:
    """How a run would reach this session: (mode, reason)."""
    if session.get("live") and not session.get("keepalive_name"):
        return "external", "A process outside Corral owns this session; resuming would fork its history."
    if session.get("keepalive_name"):
        return "hosted", "Corral hosts this session; input goes to its existing terminal."
    return "resume", "The session is stopped; Corral resumes it natively in a hosted terminal."


def target_payload(session: dict) -> dict:
    mode, reason = control_capability(session)
    ref = session_key(session)
    stable = {
        "session_ref": ref,
        "cwd": str(session.get("cwd") or ""),
        "history": str(session.get("path") or ""),
        "host": session.get("keepalive_name") or None,
        "mode": mode,
    }
    return {
        "session_ref": ref,
        "runtime": session.get("source"),
        "cwd": stable["cwd"],
        "history_path": stable["history"],
        "control": mode,
        "control_reason": reason,
        "version": "t1:" + digest(stable)[:20],
    }


def validate_directory(value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ControlError("usage_error", "--cwd must be an absolute project path.", EXIT_USAGE)
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ControlError("invalid_directory", f"The project directory does not exist: {resolved}", EXIT_NOT_FOUND)
    return str(resolved)


def fresh_plan(runtime, cwd: str, model: str | None):
    """Adapter-owned launch arguments; a model applies to this launch only."""
    from corral.runtime import LaunchError

    plan = runtime.build_new_session_plan(cwd)
    if model:
        try:
            plan = runtime.with_model(plan, model)
        except LaunchError as exc:
            raise ControlError(
                "model_selection_unsupported",
                str(exc),
                EXIT_USAGE,
                hint="Omit --model to inherit the runtime's own default, or pick another runtime.",
            ) from exc
    return plan


def self_target(session: dict, caller: dict) -> bool:
    name = str(session.get("keepalive_name") or "")
    return bool(caller.get("pane_ident")) and name.endswith("-" + str(caller["pane_ident"]))


# -- shared-library plan delegation -------------------------------------------
#
# Pure adapters mapping the locked ``agent_session_control`` runtime contract
# onto Corral's adapter-owned launch arguments. No process is started, no
# history is scanned and no global runtime configuration is changed. The
# generic plan shape is the library's; every argv rule stays in the runtime
# adapters (host-owned), including product-default approval flags, Pi hosted
# identity binding and per-adapter model/session-id support.


def control_profile(runtime) -> dict:
    """Host-owned capability profile for one runtime adapter.

    Prefers the adapter's own :meth:`control_profile` so third-party adapters
    without it still get an honest profile derived from public attributes.
    """
    profile = getattr(runtime, "control_profile", None)
    if callable(profile):
        return profile()
    return {
        "runtime": getattr(runtime, "id", "?"),
        "display_name": getattr(runtime, "display_name", "") or "",
        "executable": getattr(runtime, "executable", "") or "",
        "executable_aliases": tuple(getattr(runtime, "executable_aliases", ()) or ()),
        "available": bool(runtime.is_available()) if hasattr(runtime, "is_available") else False,
        "interrupt_keys": tuple(getattr(runtime, "interrupt_keys", ("Escape",))),
        "auto_approve_args": tuple(getattr(runtime, "auto_approve_args", ()) or ()),
        "model_supported": getattr(runtime, "model_flag", None) is not None,
        "session_id_supported": getattr(runtime, "session_id_flag", None) is not None,
        "continue_supported": False,
        "fork_supported": False,
        "mcp_supported": False,
    }


def _reject_mcp(mcp_servers) -> None:
    if mcp_servers:
        raise ControlError(
            "mcp_unsupported",
            "Per-session MCP configuration is not supported by Corral plans.",
            EXIT_USAGE,
            hint="Launch without mcp_servers; configure the runtime out of band if needed.",
        )


def library_create_plan(
    runtime,
    cwd: str,
    *,
    model: str | None = None,
    native_id: str | None = None,
    auto_approve: bool = False,
    mcp_servers=(),
) -> dict:
    """Pure fresh-launch plan; adapter semantics preserved, nothing executed.

    ``auto_approve`` records the host's explicit approval request. Corral's
    product default already bakes adapter approval flags into the plan; this
    function never invents new approval flags and never strips the adapter's
    own Claude root/sandbox handling. ``native_id`` binds the preassigned
    native identity (Pi pins ``--session-id`` to its hosted ident); runtimes
    without session-id support fail instead of silently dropping identity.
    """
    _reject_mcp(mcp_servers)
    plan = fresh_plan(runtime, cwd, model)
    _ = auto_approve  # annotation only; approval policy stays adapter-owned (see docstring).
    if native_id:
        bound = runtime.with_session_id(plan, native_id)
        if bound is not None:
            plan = bound
        elif getattr(runtime, "id", None) == "pi":
            from corral.runtime.pi import bind_hosted_ident

            plan = bind_hosted_ident(plan, native_id)
        else:
            raise ControlError(
                "session_id_unsupported",
                f"{getattr(runtime, 'id', '?')} does not accept a preassigned native session id.",
                EXIT_USAGE,
                hint="Omit native_id to let the runtime assign identity on first write.",
            )
    return {"argv": tuple(plan.argv), "cwd": plan.cwd, "env": {}}


def library_resume_plan(runtime, session: dict, *, auto_approve: bool = False, mcp_servers=()) -> dict:
    """Pure native-resume plan for the exact session mapping; never a blank replacement.

    ``session`` must carry the exact ``id``/``path``/``cwd`` fields; nothing is
    rescanned to guess it. Missing identity fails rather than creating a new
    conversation. Adapter resume arguments (including product-default approval
    flags) are preserved verbatim.
    """
    _reject_mcp(mcp_servers)
    if not isinstance(session, dict) or not session.get("id") or not session.get("path"):
        raise ControlError(
            "usage_error",
            "Resume needs the exact session mapping with id/path/cwd.",
            EXIT_USAGE,
            hint="Resolve the session first (search/show), then pass its full record.",
        )
    _ = auto_approve  # annotation only; see library_create_plan.
    plan = runtime.build_resume_plan(session)
    return {"argv": tuple(plan.argv), "cwd": plan.cwd, "env": {}}


class _AdapterProfile:
    """Library-port profile backed by a Corral runtime adapter.

    Plan construction stays entirely adapter-owned (argv rules, approval
    flags, model/session-id support, Pi hosted binding); this shim only
    translates the call shape the shared controller port expects.
    """

    def __init__(self, runtime):
        self._runtime = runtime
        self.id = runtime.id

    @property
    def interrupt_keys(self):
        return tuple(getattr(self._runtime, "interrupt_keys", ("Escape",)) or ("Escape",))

    @property
    def readiness_markers(self):
        return ()

    @property
    def composer_region(self):
        return getattr(self._runtime, "composer_region", None)

    def build_create_plan(self, cwd, *, model=None, auto_approve=False, mcp_servers=()):
        plan = library_create_plan(self._runtime, cwd, model=model, auto_approve=auto_approve, mcp_servers=mcp_servers)
        from agent_session_control.runtime import LaunchPlan

        return LaunchPlan(argv=plan["argv"], cwd=plan["cwd"], env=plan["env"])

    def build_resume_plan(self, session, *, auto_approve=False, mcp_servers=()):
        _reject_mcp(mcp_servers)
        plan = self._runtime.build_resume_plan(dict(session))
        from agent_session_control.runtime import LaunchPlan

        return LaunchPlan(argv=tuple(plan.argv), cwd=plan.cwd, env={})

    def describe(self):
        profile = control_profile(self._runtime)
        return {
            "steer": False,
            "interrupt_keys": list(profile["interrupt_keys"]),
            "model_selection": bool(profile["model_supported"]),
            "mcp": "unsupported",
        }


def host_runtime_profiles(registry=None) -> dict:
    """Adapter-backed profiles for shared-port consumers (never Corral's own launches).

    Corral's ordinary launches keep using the adapters directly; this registry
    exists so library-port callers (controller, external consumers) get real
    Corral argv rules instead of generic approximations.
    """
    from corral.runtime.registry import ACTIVE_RUNTIME_IDS as _active
    from corral.runtime.registry import default_registry

    registry = registry or default_registry()
    return {runtime.id: _AdapterProfile(runtime) for runtime in registry if runtime.id in _active}
