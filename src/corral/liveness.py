"""会话托管存活性与扫描后托管标注（运行时无关）。

本模块不感知任何具体运行时，和 `keepalive` / `embed` 同属运行时无关层。
判活查询（`is_alive` / `note_alive` / `forget_alive`）与扫描后按 pid 祖先链
贴上 `keepalive_name`（`annotate`）都在这里；启动包装仍在 keepalive。

匹配保活会话到已扫描出的会话时，优先走 pid 祖先链：`claude --resume`
之类的原生恢复可能在内部 fork/重新注册进程，pane 里的顶层 pid 未必等于
运行时自己记录的"活跃 pid"。因此用一次 `ps -eo pid,ppid` 建出整机父子
关系表，逐个候选 pid 向上追祖先链，只要能追到某个 tmux pane 的顶层 pid，
就判定命中——对是否发生过 fork 免疫。扫描没标出 pid 时（Pi claim 过期、
jsonl 已关）仍按 ``corral-<runtime>-<ident>`` 唯一命中贴名，避免把还在跑
的托管会话画成 Enter restart。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from corral import keepalive
from corral.legacy_names import (
    ALL_SESSION_PREFIXES,
    ALL_SOCKET_NAMES,
    is_managed_session,
    tmux_argv_for_session,
    tmux_base_argv,
)

_CALL_TIMEOUT = 1.5
_MAX_ANCESTOR_DEPTH = 20
_CODEX_THREAD_ID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)

_ANTIGRAVITY_ENV_ROOTS = ("ANTIGRAVITY_APP_DATA_DIR", "ANTIGRAVITY_HOME")
_ANTIGRAVITY_DIR_NAMES = (
    "antigravity-cli",
    "antigravity",
    "antigravity-ide",
    "antigravity-acp",
)
_ANTIGRAVITY_NON_TUI_SUBCOMMANDS = frozenset({
    "acp",
    "agent",
    "agents",
    "changelog",
    "help",
    "install",
    "mcp",
    "mic-serve",
    "models",
    "plugin",
    "plugins",
    "remote-control",
    "update",
})
_ANTIGRAVITY_HEADLESS_FLAGS = frozenset({
    "-p",
    "--print",
    "--prompt",
    "-h",
    "--help",
    "-v",
    "-V",
    "--version",
})
_ANTIGRAVITY_FLAGS_WITH_VALUE = frozenset({
    "--add-dir",
    "--agent",
    "--conversation",
    "--effort",
    "--input-format",
    "--json-schema",
    "--log-file",
    "--mode",
    "--model",
    "--output-format",
    "--print-timeout",
    "--project",
    "--remote-agent",
    "--sandbox",
    "--settings",
    "-i",
    "--prompt-interactive",
})
# annotate + adopt_foreign 在同一轮合并里可能各列一次 tmux；用「本轮波次」
# 字典去重，禁止跨刷新 / 跨单测的 TTL 缓存（否则 mock 的 list-sessions 会串味）。
_tmux_list_wave: dict[str, list[list[str]]] | None = None
_tmux_list_lock = threading.Lock()

# 会话名 → 最近一次「确认它还活着」的单调时钟读数。抓帧、状态查询、开通道
# 成功本身就是存活证据，记下来给界面层复用：切换会话时的活跃判定不必再 fork
# 一次 `has-session`（实测每次约 5ms，分屏几格就乘几，全压在 Textual 主线程上）。
_alive_marks: dict[str, float] = {}
_alive_lock = threading.Lock()


def note_alive(name: str) -> None:
    """登记一次「刚刚确认它活着」。只有真正拿到 tmux 正常响应时才调用。"""
    if not name:
        return
    with _alive_lock:
        _alive_marks[name] = time.monotonic()


def forget_alive(name: str) -> None:
    """确认会话已不存在时清除存活证据，避免缓存把死会话续命。"""
    with _alive_lock:
        _alive_marks.pop(name, None)


def is_alive(name: str, *, max_age: float | None = None) -> bool:
    """托管会话是否还活着（pane 里的进程退出后 tmux 会话随之消失）。

    `max_age` 给出可接受的证据陈旧上限（秒）：这段时间内有过成功抓帧 / 状态查询
    就直接返回 True，不再 fork。判定「会话是否已结束」这类必须拿准的场景一律
    不要传 `max_age`——缓存只能加速「确认活着」，不能替代宣告死亡。
    `has-session` 超时是未知，不是死亡：切走实时格时 tmux 忙碌很容易超时，
    把超时当成 False 会清掉本窗口 hosted，回来就画成已结束预览。
    """
    if max_age is not None and name:
        with _alive_lock:
            marked = _alive_marks.get(name)
        if marked is not None and (time.monotonic() - marked) <= max_age:
            return True
    if shutil.which("tmux") is None:
        return False
    try:
        subprocess.run(
            [*tmux_argv_for_session(name), "has-session", "-t", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_CALL_TIMEOUT, check=True,
            env=keepalive.tmux_env(),
        )
    except subprocess.TimeoutExpired:
        # Unknown ≠ dead. Switching away from a live pane often coincides with
        # a busy tmux; treating timeout as death pops hosted and the next
        # follow paints Ended preview for a session that is still running.
        with _alive_lock:
            return name in _alive_marks
    except (OSError, subprocess.CalledProcessError):
        forget_alive(name)
        return False
    note_alive(name)
    return True


def _socket_may_exist(socket: str) -> bool:
    """False only when the tmux socket file is provably absent.

    ``keepalive.tmux_env`` pins ``TMUX_TMPDIR=/tmp``, so tmux serves ``-L name``
    from ``/tmp/tmux-<uid>/name``. Unknown layouts (no such directory, no uid)
    keep the old behavior of asking tmux.
    """
    try:
        directory = f"/tmp/tmux-{os.getuid()}"
    except AttributeError:  # pragma: no cover - non-POSIX
        return True
    if not os.path.isdir(directory):
        return True
    return os.path.exists(os.path.join(directory, socket))


def _list_tmux_sessions(fields: str) -> list[list[str]]:
    """列出新旧保活 socket 上的托管会话；某个 socket 还不存在时跳过，不报错。"""
    if shutil.which("tmux") is None:
        return []
    with _tmux_list_lock:
        wave = _tmux_list_wave
        if wave is not None and fields in wave:
            return [list(row) for row in wave[fields]]
    rows: list[list[str]] = []
    seen: set[str] = set()
    for socket in ALL_SOCKET_NAMES:
        if not _socket_may_exist(socket):
            continue  # no server behind it: skip a ~30 ms failing tmux spawn
        try:
            out = subprocess.check_output(
                [*tmux_base_argv(socket), "list-sessions", "-F", fields],
                stderr=subprocess.DEVNULL,
                timeout=keepalive.SUBPROCESS_TIMEOUT,
                env=keepalive.tmux_env(),
            ).decode()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        for line in out.splitlines():
            parts = line.split("|")
            if not parts or not is_managed_session(parts[0]) or parts[0] in seen:
                continue
            seen.add(parts[0])
            rows.append(parts)
    with _tmux_list_lock:
        if _tmux_list_wave is not None:
            _tmux_list_wave[fields] = [list(row) for row in rows]
    return rows


@contextmanager
def tmux_list_wave():
    """Reuse ``list-sessions`` results within one store merge; no cross-refresh TTL."""
    global _tmux_list_wave
    with _tmux_list_lock:
        _tmux_list_wave = {}
    try:
        yield
    finally:
        with _tmux_list_lock:
            _tmux_list_wave = None


def clear_tmux_list_cache() -> None:
    """Tests: drop any in-flight wave cache."""
    global _tmux_list_wave
    with _tmux_list_lock:
        _tmux_list_wave = None


def _build_ppid_map() -> dict[int, int]:
    """一次 `ps -eo pid,ppid` 拿到整机父子关系，供祖先链匹配复用；跨 macOS/Linux 通用。"""
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,ppid"], stderr=subprocess.DEVNULL, timeout=keepalive.SUBPROCESS_TIMEOUT
        ).decode()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}
    mapping: dict[int, int] = {}
    for line in out.splitlines()[1:]:  # 跳过表头
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            mapping[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return mapping


def _is_descendant(pid: int, ancestor_pid: int, ppid_map: dict[int, int]) -> bool:
    current = pid
    for _ in range(_MAX_ANCESTOR_DEPTH):
        if current == ancestor_pid:
            return True
        parent = ppid_map.get(current)
        if parent is None or parent <= 1:
            return False
        current = parent
    return False


def _parse_managed_session_name(name: str) -> tuple[str, str] | None:
    """从 ``corral-pi-abcd1234`` / ``sc-claude-…`` 拆出 (runtime_id, ident)。"""
    for prefix in ALL_SESSION_PREFIXES:
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix):]
        runtime, sep, ident = rest.rpartition("-")
        if sep and runtime and ident:
            return runtime, ident
    return None


def list_managed_hosts() -> list[dict]:
    """Live managed tmux panes with runtime/ident/cwd for cross-process adoption.

    Phone remote hosts panes in a separate daemon; the desktop TUI never sees
    that process's ``register_hosted_session`` cards. Listing here lets the store
    invent matching provisional cards before history exists on disk.

    Unit tests set ``CORRAL_ISOLATE_MANAGED_HOSTS=1`` so a developer machine's
    live keepalive panes do not leak into empty-scan fixtures as foreign cards.
    """
    if os.environ.get("CORRAL_ISOLATE_MANAGED_HOSTS") == "1":
        return []
    hosts: list[dict] = []
    for row in _list_tmux_sessions(
        "#{session_name}|#{pane_pid}|#{pane_current_path}|#{session_created}"
    ):
        if not row:
            continue
        name = row[0]
        parsed = _parse_managed_session_name(name)
        if parsed is None:
            continue
        runtime_id, ident = parsed
        pane_pid = None
        if len(row) > 1 and row[1]:
            try:
                pane_pid = int(row[1])
            except ValueError:
                pane_pid = None
        cwd = row[2].strip() if len(row) > 2 and row[2] else ""
        try:
            created_at = float(row[3]) if len(row) > 3 else None
        except ValueError:
            created_at = None
        hosts.append(
            {
                "name": name,
                "runtime_id": runtime_id,
                "ident": ident,
                "cwd": cwd or None,
                "pane_pid": pane_pid,
                "created_at": created_at,
            }
        )
    return hosts


def _ident_matches_id(ident: str, sid: str) -> bool:
    if not ident or not sid:
        return False
    if sid == ident or sid.startswith(ident):
        return True
    compact = sid.replace("-", "")
    return compact.startswith(ident) or ident.startswith(compact[:8])


def _name_matches_session(name: str, session: dict) -> bool:
    """托管名末段 ident 是否对得上这条会话 id（占位 8 位或完整 id）。

    Also accepts ids this card absorbed from superseded continuation files
    (``continued_from``): the hosted pane keeps the name it was launched
    with while the conversation moves on to a new history file.
    """
    ident = str(name or "").rsplit("-", 1)[-1]
    ids = [str(session.get("id") or ""), *(session.get("continued_from") or ())]
    return any(_ident_matches_id(ident, str(sid)) for sid in ids)


def _codex_resumed_thread_id(pane_pid: int) -> str | None:
    """只从活着的旧版 Codex pane 启动命令读取完整恢复线程 ID。"""
    try:
        command = subprocess.check_output(
            ["ps", "-ww", "-p", str(pane_pid), "-o", "command="],
            stderr=subprocess.DEVNULL,
            timeout=keepalive.SUBPROCESS_TIMEOUT,
        ).decode().strip()
        argv = shlex.split(command)
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    for index, arg in enumerate(argv[:-1]):
        if os.path.basename(arg) != "codex" or argv[index + 1] != "resume":
            continue
        ids = [value for value in argv[index + 2:] if _CODEX_THREAD_ID.fullmatch(value)]
        return ids[0] if len(ids) == 1 else None
    return None


def _antigravity_root_from_path(raw_path: object) -> Path | None:
    """Derive `<root>` from an Antigravity history, DB, or lock file path."""
    text = str(raw_path or "").strip()
    if not text:
        return None
    parts = Path(text).parts
    for marker in ("brain", "conversations", "presence"):
        for idx in range(len(parts) - 1, 0, -1):
            if parts[idx] == marker:
                return Path(*parts[:idx])
    return None


def _standard_antigravity_roots() -> list[Path]:
    """Candidate Antigravity state roots in preference order (deduplicated)."""
    home = Path.home()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    raw: list[Path] = []
    for env_key in _ANTIGRAVITY_ENV_ROOTS:
        val = (os.environ.get(env_key) or "").strip()
        if val:
            raw.append(Path(val).expanduser())
    for dirname in _ANTIGRAVITY_DIR_NAMES:
        raw.append(home / ".gemini" / dirname)
    raw.append(xdg / "antigravity")
    seen: set[str] = set()
    out: list[Path] = []
    for candidate in raw:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def _antigravity_presence_dirs(sessions=(), roots: list[Path] | tuple[Path, ...] | None = None) -> list[Path]:
    """Resolve `presence/` directories to inspect without leaking host dirs into fixtures."""
    if roots is not None:
        return [Path(root) / "presence" for root in roots]
    standard = _standard_antigravity_roots()
    standard_resolved = {os.path.realpath(str(r)) for r in standard}
    derived: list[Path] = []
    seen: set[str] = set()
    for session in sessions:
        if not isinstance(session, dict):
            continue
        root = _antigravity_root_from_path(session.get("path"))
        if root is not None:
            key = os.path.realpath(str(root))
            if key not in seen:
                seen.add(key)
                derived.append(root)
    if derived and not any(os.path.realpath(str(r)) in standard_resolved for r in derived):
        # Test fixtures under tmp_path use isolated roots; include explicit env roots too.
        extra = [
            Path((os.environ.get(k) or "").strip()).expanduser()
            for k in _ANTIGRAVITY_ENV_ROOTS
            if (os.environ.get(k) or "").strip()
        ]
        combined = derived + [r for r in extra if os.path.realpath(str(r)) not in seen]
        return [r / "presence" for r in combined]
    combined = list(derived)
    for r in standard:
        key = os.path.realpath(str(r))
        if key not in seen:
            seen.add(key)
            combined.append(r)
    return [r / "presence" for r in combined]


def _parse_antigravity_argv(argv: list[str]) -> tuple[bool, str | None]:
    """Return ``(is_interactive_tui, explicit_conversation_id)`` for one `agy` argv."""
    if not argv:
        return False, None
    exe = os.path.basename(argv[0].lstrip("-")).lower()
    if exe not in {"agy", "agy.exe"}:
        return False, None
    args = argv[1:]
    conversation_id: str | None = None
    first_positional: str | None = None
    idx = 0
    while idx < len(args):
        token = args[idx]
        if token in _ANTIGRAVITY_HEADLESS_FLAGS:
            return False, None
        if token.startswith(("--print=", "--prompt=")):
            return False, None
        if token == "--conversation":
            if idx + 1 < len(args) and args[idx + 1]:
                conversation_id = args[idx + 1]
                idx += 2
                continue
        elif token.startswith("--conversation="):
            conversation_id = token.split("=", 1)[1] or None
        elif token in _ANTIGRAVITY_FLAGS_WITH_VALUE:
            idx += 2
            continue
        elif not token.startswith("-") and first_positional is None:
            first_positional = token
        idx += 1
    if first_positional in _ANTIGRAVITY_NON_TUI_SUBCOMMANDS:
        return False, None
    return True, conversation_id


def _list_antigravity_processes() -> tuple[set[int], dict[str, int]] | None:
    """Return ``(interactive_agy_pids, conversation_id -> pid_from_argv)`` via `ps`.

    ``None`` means the probe itself failed (timeout/error), which callers must
    not confuse with "no live agy process".
    """
    try:
        output = subprocess.check_output(
            ["ps", "-eo", "pid=,args="],
            timeout=_CALL_TIMEOUT, stderr=subprocess.DEVNULL,
        ).decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    live_pids: set[int] = set()
    argv_cids: dict[str, int] = {}
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        try:
            argv = shlex.split(parts[1])
        except ValueError:
            argv = parts[1].split()
        interactive, cid = _parse_antigravity_argv(argv)
        if not interactive:
            continue
        live_pids.add(pid)
        if cid and cid not in argv_cids:
            argv_cids[cid] = pid
    return live_pids, argv_cids


def _parse_lsof_lock_fds(output: str) -> list[tuple[int, str, str]]:
    """Parse ``lsof -F pftan`` lines into ``(pid, access, path)`` for regular ``.lock`` fds."""
    records: list[tuple[int, str, str]] = []
    cur_pid: int | None = None
    fd_open = False
    fd_type = ""
    fd_access = ""
    fd_name = ""

    def _flush() -> None:
        if cur_pid is not None and fd_open and fd_type == "REG" and fd_name.endswith(".lock"):
            records.append((cur_pid, fd_access, fd_name))

    for line in output.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            _flush()
            fd_open = False
            fd_type = fd_access = fd_name = ""
            try:
                cur_pid = int(value)
            except ValueError:
                cur_pid = None
        elif tag == "f":
            _flush()
            fd_open = True
            fd_type = fd_access = fd_name = ""
        elif tag == "t":
            fd_type = value
        elif tag == "a":
            fd_access = value
        elif tag == "n":
            fd_name = value
    _flush()
    return records


def _list_lsof_lock_fds(presence_dirs: list[Path]) -> list[tuple[int, str, str]] | None:
    """List open regular ``.lock`` file descriptors across existing ``presence/`` directories.

    ``None`` means an ``lsof`` probe failed (timeout/OS error) for a directory
    that holds lock files, so the result would be incomplete. A non-zero exit
    with no output is normal (``lsof`` exits 1 when nothing is open).
    """
    if not presence_dirs or shutil.which("lsof") is None:
        return []
    records: list[tuple[int, str, str]] = []
    for presence_dir in presence_dirs:
        if not presence_dir.is_dir():
            continue
        try:
            with os.scandir(presence_dir) as entries:
                has_locks = any(entry.name.endswith(".lock") for entry in entries)
        except OSError:
            continue
        if not has_locks:
            continue
        try:
            proc = subprocess.run(
                ["lsof", "-F", "pftan", "+D", str(presence_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=_CALL_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.stdout:
            records.extend(_parse_lsof_lock_fds(proc.stdout))
    return records


def _resolve_antigravity_live_pids_checked(
    sessions=(),
    roots: list[Path] | tuple[Path, ...] | None = None,
) -> dict[str, int] | None:
    """Like :func:`resolve_antigravity_live_pids`, but ``None`` when a probe failed."""
    processes = _list_antigravity_processes()
    if processes is None:
        return None
    live_pids, argv_cids = processes
    if not live_pids:
        return {}
    presence_dirs = _antigravity_presence_dirs(sessions=sessions, roots=roots)
    lock_fds = _list_lsof_lock_fds(presence_dirs)
    if lock_fds is None:
        return None
    resolved: dict[str, int] = {}
    # Prefer read-write/write descriptors (`u` / `w`) before read-only if multiple exist.
    for preferred in (True, False):
        for pid, access, lock_path in lock_fds:
            if pid not in live_pids:
                continue
            is_write = access in {"u", "w"}
            if is_write != preferred:
                continue
            cid = Path(lock_path).stem
            if cid and cid not in resolved:
                resolved[cid] = pid
    for cid, pid in argv_cids.items():
        if cid and cid not in resolved:
            resolved[cid] = pid
    return resolved


def resolve_antigravity_live_pids(
    sessions=(),
    roots: list[Path] | tuple[Path, ...] | None = None,
) -> dict[str, int]:
    """Map live interactive `agy` ``conversation_id`` -> ``pid``.

    Combines open `REG` descriptors on ``presence/<cid>.lock`` (via ``lsof``,
    skipping ``DIR`` watches and dead unlocked lock files) with `ps` argv
    validation and ``--conversation <cid>`` fallback so multiple bare `agy`
    processes in the same cwd map 1-to-1 to their exact conversations.
    Returns ``{}`` when a probe failed; use the checked variant to tell apart.
    """
    return _resolve_antigravity_live_pids_checked(sessions, roots=roots) or {}


def annotate_antigravity_sessions(
    sessions,
    *,
    roots: list[Path] | tuple[Path, ...] | None = None,
    force_override: bool = False,
) -> None:
    """Populate exact ``live`` / ``pid`` fields on Antigravity session dicts."""
    ag_sessions = [
        s for s in sessions
        if isinstance(s, dict) and str(s.get("source") or "") == "antigravity"
        and not s.get("provisional")
    ]
    if not ag_sessions:
        return
    if not force_override:
        # Inside liveness.annotate(), skip live probing only when no presence dir exists
        # on disk AND every synthetic test session already supplied a mock pid.
        dirs = _antigravity_presence_dirs(sessions=ag_sessions, roots=roots)
        has_presence_dir = any(d.is_dir() for d in dirs)
        if not has_presence_dir and all(s.get("pid") is not None for s in ag_sessions):
            return
    live_map = _resolve_antigravity_live_pids_checked(ag_sessions, roots=roots)
    if live_map is None:
        # A failed ps/lsof probe is "unknown", not "nothing is live": keep the
        # scanner's existing live/pid so clients do not flash sessions as dead.
        return
    dirs_present = force_override or any(
        d.is_dir() for d in _antigravity_presence_dirs(sessions=ag_sessions, roots=roots)
    )
    for session in ag_sessions:
        cid = str(session.get("id") or "")
        pid = live_map.get(cid)
        if pid is not None:
            session["live"] = True
            session["pid"] = pid
        elif dirs_present:
            session["live"] = False
            session["pid"] = None


def annotate(sessions, provisional_names: set[str] | None = None) -> None:
    """给命中保活的会话就地加上 `keepalive_name` 字段；不生成新列表，不改变顺序。

    同一时刻一个进程只属于一个 pane：已经对上某个 pane 的 pid 不再参与后续
    pane 的匹配（否则进程树嵌套时后通到的 pane 会把名字改写成别人的）。同
    一 pane 也只挂一条会话：扫描若把父进程和子进程绑到两张卡上，祖先链会让
    两张卡都命中同一份 tmux 画面，分屏两格就会一模一样。同一 pane 有多个
    候选时优先「pid 恰好就是 pane 顶层进程」的精确命中，再考虑祖先链。

    扫描没标出 pid 时仍要按 tmux 名贴回去。Pi 的 jsonl 写完即关、claim 过期
    后 `_apply_live_flags` 经常拿不到 pid；旧逻辑这里直接 return，侧栏就把还
    在跑的托管会话画成 Enter restart，回车却走 ``new-session -A`` 接回原进程。

    `provisional_names` 是本进程刚建 pane 的托管名集合（年轻占位卡）：这些
    pane 只接受 pid 精确相等，祖先链不再往上贴——agent 尚未注册 pid 时祖先链
    只有误伤、没有收益（短命 `arch`/Rosetta 探测子进程曾把整卡贴错 pane）。
    为 None 时年轻豁免关闭（调用方不感知占位态，如 agent 只读查询），但
    OpenCode 预钉 pane 的祖先链跳过依然生效（见下）。

    OpenCode 祖先链只对“有 claim 的预钉 pane”跳过：SessKit 已凭预钉 ``-s``
    精确绑定，DB 行未落盘时会话根本不在候选里，祖先链只会把短命子进程误绑
    上来。预钉前老 pane（无 claim）保留祖先链兼容，否则子进程模型里永不贴名。
    """
    if not sessions:
        return
    annotate_antigravity_sessions(sessions, force_override=False)
    young = set(provisional_names or ())
    candidates = {s.get("pid"): s for s in sessions if s.get("pid")}
    tmux_sessions = _list_tmux_sessions("#{session_name}|#{pane_pid}|#{pane_current_command}")
    if not tmux_sessions:
        return

    assigned_pids: set[int] = set()
    assigned_names: set[str] = set()
    _claim_exists_memo: dict[str, bool] = {}

    def _pane_has_opencode_claim(name: str) -> bool:
        """该 pane 名是否有预钉 claim（单轮 annotate 内按名 memo）。"""
        if name in _claim_exists_memo:
            return _claim_exists_memo[name]
        exists = False
        parsed = _parse_managed_session_name(name)
        if parsed is not None and parsed[0] == "opencode":
            try:
                from corral import opencode_identity

                exists = opencode_identity.ses_id_for_ident(parsed[1]) is not None
            except ImportError:  # pragma: no cover - 包结构异常时按老 pane 兼容
                exists = False
        _claim_exists_memo[name] = exists
        return exists

    def _claim(name: str, pane_pid: int, *, exact_only: bool) -> None:
        if name in assigned_names:
            return
        matches: list[tuple[int, dict]] = []
        for pid, session in candidates.items():
            if pid in assigned_pids:
                continue
            if pid == pane_pid:
                # 精确命中永远生效（含年轻 pane 与 OpenCode）。
                matches.append((pid, session))
                continue
            if exact_only or name in young:
                # 年轻占位 pane 不走祖先链：agent 尚未注册 pid 时只有误伤、
                # 没有收益（短命 arch/Rosetta 探测子进程曾把整卡贴错 pane）。
                continue
            if str((session or {}).get("source") or "") == "opencode":
                # 有 claim 的预钉 pane 只走精确（SessKit -s 已绑定，DB 未落盘
                # 时会话不在候选里，祖先链只会误绑短命子进程）；无 claim 老 pane
                # 保留祖先链兼容。
                if _pane_has_opencode_claim(name):
                    continue
            if _is_descendant(pid, pane_pid, ppid_map):
                matches.append((pid, session))
        if not matches:
            return
        pid, session = matches[0]
        session["keepalive_name"] = name
        assigned_pids.add(pid)
        assigned_names.add(name)

    if candidates:
        ppid_map = _build_ppid_map()
        # 第一遍只认「pid 恰好就是 pane 顶层进程」的精确命中（进程树嵌套时，深祖
        # 先链命中的可能是外层 pane，精确命中才是进程真正性所在的那个 pane）。
        for row in tmux_sessions:
            if len(row) < 2:
                continue
            name, pane_pid_text = row[0], row[1]
            try:
                pane_pid = int(pane_pid_text)
            except ValueError:
                continue
            _claim(name, pane_pid, exact_only=True)
        # 第二遍祖先链兜底；已对上 pane 的 pid / 名字不再改绑：一个进程只挂一个
        # 名字，一个 pane 也只挂一条会话。
        for row in tmux_sessions:
            if len(row) < 2:
                continue
            name, pane_pid_text = row[0], row[1]
            try:
                pane_pid = int(pane_pid_text)
            except ValueError:
                continue
            _claim(name, pane_pid, exact_only=False)

    _annotate_unmatched_by_session_name(sessions, tmux_sessions, assigned_names)


def _annotate_unmatched_by_session_name(
    sessions, tmux_sessions: list[list[str]], assigned_names: set[str],
) -> None:
    """pid 没贴上时，用 wrap_plan 同款的 ``corral-<runtime>-<ident>`` 名接回还活着的 pane。"""
    unnamed = [session for session in sessions if not session.get("keepalive_name")]
    if not unnamed:
        return
    for row in tmux_sessions:
        if not row:
            continue
        name = row[0]
        if name in assigned_names:
            continue
        parsed = _parse_managed_session_name(name)
        if parsed is None:
            continue
        runtime, _ident = parsed
        if runtime == "opencode":
            # OpenCode 短 tmux ident（8 位随机）与 ses_… 无对应关系，不得按名
            # 前缀匹配（与 SessKit _session_for_host_ident 的 16 字符门限对齐；
            # 今天恒为 False 的比较一旦放宽就会变成前缀碰撞误绑）。精确通道
            # 只有两条：SessKit 的 cmdline -s（pid 路已处理）与这里的 claim。
            try:
                from corral import opencode_identity
            except ImportError:  # pragma: no cover - 包结构异常时退回不贴名
                continue
            expected = opencode_identity.ses_id_for_ident(_ident)
            if not expected:
                continue
            matches = [
                session for session in unnamed
                if str(session.get("source") or "") == "opencode"
                and str(session.get("id") or "") == expected
            ]
            if len(matches) == 1:
                matches[0]["keepalive_name"] = name
                assigned_names.add(name)
                unnamed.remove(matches[0])
            continue
        if runtime == "codex":
            # 旧版直连 pane 没有 claim；仅完整 resume UUID 能证明归属。
            # 八位 tmux ident 不能单独绑定历史，避免串到相邻会话。
            if len(row) > 2 and row[2] not in {"codex", "node"}:
                continue
            try:
                pane_pid = int(row[1])
            except (IndexError, ValueError):
                continue
            thread_id = _codex_resumed_thread_id(pane_pid)
            matches = [
                session for session in unnamed
                if session.get("source") == "codex" and session.get("id") == thread_id
            ]
            if len(matches) == 1:
                matches[0]["keepalive_name"] = name
                assigned_names.add(name)
                unnamed.remove(matches[0])
            continue
        matches = [
            session
            for session in unnamed
            if str(session.get("source") or "") == runtime
            and _name_matches_session(name, session)
        ]
        if len(matches) != 1:
            continue
        matches[0]["keepalive_name"] = name
        assigned_names.add(name)
        unnamed.remove(matches[0])


# 在 liveness 加载完成后绑定，避免与 keepalive 顶层互相 import 形成环。
keepalive.annotate = annotate
