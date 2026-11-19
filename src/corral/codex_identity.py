"""Codex 托管会话的创建回执：只接受包装器报告的完整 thread id。"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path

CLAIM_DIR = Path.home() / ".cache" / "corral" / "codex-claims"
CLAIM_PATH_ENV = "CORRAL_CODEX_CLAIM_PATH"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

_ClaimStamp = tuple[int, int, int, int, int]
_CLAIM_MEMO_LIMIT = 2048
_CLAIM_MEMO_LOCK = threading.Lock()
_CLAIM_MEMO: OrderedDict[str, tuple[_ClaimStamp, tuple[str, str, int]]] = OrderedDict()


def _claim_stamp(info: os.stat_result) -> _ClaimStamp:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def claim_env_pairs(ident: str) -> list[str]:
    CLAIM_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = CLAIM_DIR / f"{ident}.json"
    return ["-e", f"{CLAIM_PATH_ENV}={path}"]


def claim_signature() -> tuple[tuple[str, int, int], ...]:
    """Invalidate cached Codex scans when a pane changes its exact thread."""
    rows: list[tuple[str, int, int]] = []
    try:
        with os.scandir(CLAIM_DIR) as entries:
            for entry in entries:
                if not entry.name.endswith(".json"):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                rows.append((entry.name, st.st_mtime_ns, st.st_size))
    except OSError:
        return ()
    rows.sort()
    return tuple(rows)


def write_claim(thread_id: str, rollout_path: str, pid: int) -> bool:
    """Record only a verified root thread and its exact local rollout file."""
    claim_path = os.environ.get(CLAIM_PATH_ENV)
    if not claim_path or not _UUID.fullmatch(thread_id):
        return False
    path = Path(rollout_path).resolve()
    root = (Path.home() / ".codex" / "sessions").resolve()
    if not path.is_relative_to(root) or not path.name.endswith(f"{thread_id}.jsonl"):
        return False
    try:
        # Codex returns an exact id/path before the first prompt creates the
        # rollout. Keep the pane provisional until the file appears.
        if path.exists():
            with path.open(encoding="utf-8") as stream:
                header = stream.readline()
            if header:
                first = json.loads(header)
                if first.get("type") != "session_meta" or first.get("payload", {}).get("id") != thread_id:
                    return False
        destination = Path(claim_path)
        if destination.parent.resolve() != CLAIM_DIR.resolve() or destination.is_symlink():
            return False
        CLAIM_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {"thread_id": thread_id, "rollout_path": str(path), "pid": pid}
        fd, temp_path = tempfile.mkstemp(prefix=f".{destination.stem}-", dir=CLAIM_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, destination)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        return True
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False

def _parsed_claim(entry: os.DirEntry[str]) -> tuple[str, str, int] | None:
    try:
        before = _claim_stamp(entry.stat())
    except OSError:
        return None
    path = entry.path
    with _CLAIM_MEMO_LOCK:
        cached = _CLAIM_MEMO.get(path)
        if cached is not None and cached[0] == before:
            _CLAIM_MEMO.move_to_end(path)
            return cached[1]
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        after = _claim_stamp(os.stat(path))
        if not isinstance(data, dict):
            return None
        parsed = (str(data["thread_id"]), str(data["rollout_path"]), int(data["pid"]))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    if after == before:
        with _CLAIM_MEMO_LOCK:
            _CLAIM_MEMO[path] = (before, parsed)
            _CLAIM_MEMO.move_to_end(path)
            while len(_CLAIM_MEMO) > _CLAIM_MEMO_LIMIT:
                _CLAIM_MEMO.popitem(last=False)
    return parsed


def live_claims(sessions_dir: str) -> dict[str, int]:
    root = Path(os.path.realpath(sessions_dir))
    out: dict[str, int] = {}
    try:
        with os.scandir(CLAIM_DIR) as entries:
            claim_entries = [entry for entry in entries if entry.name.endswith(".json")]
    except OSError:
        return out
    for entry in claim_entries:
        parsed = _parsed_claim(entry)
        if parsed is None:
            continue
        thread_id, rollout_raw, pid = parsed
        if not _UUID.fullmatch(thread_id):
            continue
        try:
            os.kill(pid, 0)
        except OSError:
            continue
        try:
            rollout = Path(os.path.realpath(rollout_raw))
        except OSError:
            continue
        if not rollout.is_relative_to(root) or not rollout.name.endswith(thread_id + ".jsonl"):
            continue
        out[thread_id] = pid
    return out
