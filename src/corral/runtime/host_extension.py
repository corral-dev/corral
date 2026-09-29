"""Corral-owned host extension for SessKit scans.

SessKit core stays application-neutral: it only understands native runtime
history plus generic process evidence. Everything Corral-specific — hosted
process-environment keys, claim layouts, ``corral-``/``pickup-`` isolation
directories, the title-generation marker, handoff-wrapper peeling, the
``oc-manager-`` automation exception, and the cache directory override —
lives here and is passed explicitly per scan via :func:`corral_host_extension`.

SessKit never imports this module; each runtime adapter hands the built
extension to :func:`corral.runtime.sesskit_bridge.call_scan`, which forwards
it only to scanners that accept it (older SessKit builds simply ignore it).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

TITLE_PROMPT_MARKER = "你将看到一批编程助手会话的摘录"

TITLE_NOISE_PREFIXES: tuple[str, ...] = (
    "你将看到一批编程助手会话的摘录",
    "你是 OpenConductor 的管理者 Agent",
    "你是 OpenConductor 的聊天意图解析器",
)

EPHEMERAL_PREFIXES: tuple[str, ...] = ("oc-manager-",)

HOST_ENV_KEYS: tuple[str, ...] = (
    "CORRAL_SESSION_ID",
    "PICKUP_SESSION_ID",
    "SC_SESSION_ID",
    "CORRAL_RUNTIME",
    "PICKUP_RUNTIME",
    "SC_RUNTIME",
    "CORRAL_PI_INSTANCE_ID",
    "CORRAL_PI_CLAIM_PATH",
)

_HOST_SESSION_ID_KEYS: tuple[str, ...] = (
    "CORRAL_SESSION_ID",
    "PICKUP_SESSION_ID",
    "SC_SESSION_ID",
)

HOSTED_DIR_PREFIX = "corral-"
LEGACY_HOSTED_DIR_PREFIXES: tuple[str, ...] = ("pickup-",)

LEGACY_SCHEMA_IDS: tuple[str, ...] = ("corral.share/v1",)

_HANDOFF_TASK_RE = re.compile(r"^(?:Task|任务)\s*[:：]\s*(.+)$")
_HANDOFF_INTRO_MARKERS = (
    "You are picking up a session from",
    "你正在接力一个来自",
)
_DIGEST_HEADING_MARKERS = (
    "Below is a conversation excerpt automatically extracted",
    "以下是从原会话自动提取的对话摘录",
)
_ORIGINAL_REQUEST_MARKERS = (
    "[Original request]",
    "【原始需求】",
)
_NOISE_LINE_PREFIXES = (
    "Original session history file:",
    "Original working directory:",
    "History format hint:",
    "原会话历史文件：",
    "原工作目录：",
    "历史格式提示：",
    "Use the excerpt above as a clue",
    "请以上述摘录为线索",
    "Then inspect the actual workspace",
    "随后检查当前工作区",
    "Read the session history above first",
    "请先读取上述会话历史",
    *_DIGEST_HEADING_MARKERS,
)


def session_id_from_env(env: dict[str, str] | None) -> str:
    """Corral's hosted-session ident from a process-environment snapshot."""
    if not env:
        return ""
    for key in _HOST_SESSION_ID_KEYS:
        value = env.get(key)
        if value:
            return str(value)
    return ""


def is_isolation_dir(directory: str) -> bool:
    base = os.path.basename(str(directory or "").rstrip("/"))
    return base.startswith((HOSTED_DIR_PREFIX, *LEGACY_HOSTED_DIR_PREFIXES))


def isolation_dirname(ident: str) -> str:
    return f"{HOSTED_DIR_PREFIX}{ident}"


def hosted_session_dir(cwd: str, ident: str) -> str:
    """Corral's per-pane Pi session directory (one pane, one jsonl writer).

    Host-owned layout: default Pi cwd encoding plus the host isolation
    segment. Moved out of SessKit; SessKit only recognizes such directories
    when the extension's ``is_isolation_dir`` says so.
    """
    from corral.scan import pi as scan_pi

    return os.path.join(
        scan_pi.SESSIONS_DIR,
        scan_pi.encode_pi_session_cwd(cwd),
        isolation_dirname(ident),
    )


def _cut_at_intro(text: str) -> str:
    cut: int | None = None
    for marker in _HANDOFF_INTRO_MARKERS:
        idx = text.find(marker)
        if idx >= 0 and (cut is None or idx < cut):
            cut = idx
    if cut is None:
        return text.strip()
    return text[:cut].strip()


def _strip_original_request_prefix(line: str) -> str:
    stripped = line.strip()
    for marker in _ORIGINAL_REQUEST_MARKERS:
        if stripped.startswith(marker):
            return stripped[len(marker):].strip()
    return stripped


def _task_from_line(line: str) -> str | None:
    match = _HANDOFF_TASK_RE.fullmatch(_strip_original_request_prefix(_cut_at_intro(line)))
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _peel_wrapper_lines(text: str) -> str:
    kept: list[str] = []
    for raw_line in str(text).splitlines():
        line = _cut_at_intro(raw_line).strip()
        if not line:
            continue
        if line.startswith(_NOISE_LINE_PREFIXES):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _looks_like_nested_handoff(text: str) -> bool:
    if any(marker in text for marker in _HANDOFF_INTRO_MARKERS):
        return True
    if any(marker in text for marker in _ORIGINAL_REQUEST_MARKERS) and _HANDOFF_TASK_RE.search(
        _strip_original_request_prefix(text.splitlines()[0]) if text else ""
    ):
        return True
    return False


def split_handoff_text(text: str | None) -> tuple[str | None, str]:
    """Return ``(inherited_task, digest)`` from a Corral handoff prompt.

    Digest text excludes the pickup intro and the excerpt heading. Nested
    handoffs (an earlier pickup flattened into ``[Original request]``) peel
    inward so the inner task wins. A payload that was already extracted
    (``Task: …`` plus body, no intro) keeps the inherited task and the body.
    """
    peeled = _peel_wrapper_lines(str(text or ""))
    if not peeled:
        return None, ""

    lines = peeled.splitlines()
    inherited = _task_from_line(lines[0])
    rest = "\n".join(lines[1:]).strip()
    if rest and _looks_like_nested_handoff(rest):
        inner_inherited, inner_digest = split_handoff_text(rest)
        if inner_inherited:
            return inner_inherited, inner_digest
        if inner_digest:
            return inherited, inner_digest
    if inherited:
        return inherited, rest
    return None, peeled


def peel_handoff_text(text: str | None) -> str:
    """Apply Corral's handoff transform: digest-first text for excerpts."""
    raw = str(text or "")
    if not raw:
        return ""
    inherited, digest = split_handoff_text(raw)
    if digest or inherited:
        if inherited and digest:
            first = digest.splitlines()[0] if digest else ""
            if _task_from_line(first) == inherited:
                return digest
            return f"Task: {inherited}\n\n{digest}"
        if digest:
            return digest
        return f"Task: {inherited}"
    return raw


def codex_claim_provider(sessions_dir: str) -> Mapping[str, int]:
    """Corral Codex pane claims: exact thread id → pane pid."""
    from corral.codex_identity import live_claims

    return live_claims(sessions_dir)


def pi_claims_provider() -> list[dict]:
    """Live Corral Pi identity claims in SessKit's neutral claim shape."""
    from corral import pi_identity

    out: list[dict] = []
    for claim in pi_identity.read_claims():
        if not pi_identity.claim_is_live(claim):
            continue
        try:
            pid = int(claim.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        try:
            sequence = int(claim.get("sequence", 0) or 0)
        except (TypeError, ValueError):
            sequence = 0
        out.append(
            {
                "pid": pid,
                "session": str(claim.get("sessionId") or ""),
                "session_path": str(claim.get("sessionFile") or ""),
                "sequence": sequence,
                "instance": str(claim.get("instanceId") or ""),
            }
        )
    return out


def pi_live_map_dir() -> str:
    """Live-map base preserving the previous override chain.

    ``SESSKIT_CACHE_DIR`` first, then Corral's own ``CORRAL_CACHE_DIR``,
    then the neutral SessKit default. Host-owned ordering lives here, not
    in SessKit core. Falls back across SessKit generations (the neutral
    default moved from ``sesskit.hosted`` to ``sesskit.cache``).
    """
    for key in ("SESSKIT_CACHE_DIR", "CORRAL_CACHE_DIR"):
        override = os.environ.get(key, "").strip()
        if override:
            return str(Path(override).expanduser())
    try:
        from sesskit.cache import cache_dir
    except ImportError:
        try:
            from sesskit.hosted import cache_dir
        except ImportError:
            return str(Path.home() / ".cache" / "sesskit")
    return str(cache_dir())


def corral_host_extension(*, cache: Any | None = None) -> Any:
    """Build the per-scan SessKit host extension for Corral.

    Returns ``None`` against older SessKit builds without the seam; the
    bridge then skips the extension and scanners keep legacy behavior.
    """
    try:
        from sesskit.parsers.common import HostExtension
    except ImportError:
        return None
    from corral import pi_identity

    return HostExtension(
        name="corral",
        env_keys=HOST_ENV_KEYS,
        session_id_from_env=session_id_from_env,
        is_isolation_dir=is_isolation_dir,
        isolation_dirname=isolation_dirname,
        codex_claim_provider=codex_claim_provider,
        pi_claims_provider=pi_claims_provider,
        pi_instance_env_key=pi_identity.INSTANCE_ENV,
        pi_live_map_dir=pi_live_map_dir(),
        title_prompt_marker=TITLE_PROMPT_MARKER,
        title_noise_prefixes=TITLE_NOISE_PREFIXES,
        ephemeral_prefixes=EPHEMERAL_PREFIXES,
        excerpt_preprocess=peel_handoff_text,
        legacy_schema_ids=LEGACY_SCHEMA_IDS,
        cache=cache,
    )


def clip_for_digest(text: str | None, limit: int) -> str:
    """Handoff-aware clip used for conversation digests (flattened by caller)."""
    from sesskit.titles import EXCERPT_LIMIT, clip_user_excerpt

    peeled = peel_handoff_text(text)
    return clip_user_excerpt(peeled, limit=max(limit * 4, EXCERPT_LIMIT))


__all__ = [
    "EPHEMERAL_PREFIXES",
    "HOST_ENV_KEYS",
    "LEGACY_SCHEMA_IDS",
    "TITLE_NOISE_PREFIXES",
    "TITLE_PROMPT_MARKER",
    "clip_for_digest",
    "codex_claim_provider",
    "corral_host_extension",
    "hosted_session_dir",
    "is_isolation_dir",
    "isolation_dirname",
    "peel_handoff_text",
    "pi_claims_provider",
    "pi_live_map_dir",
    "session_id_from_env",
    "split_handoff_text",
]
