"""OpenCode 托管会话的预钉身份：launch 时生成完整 ``ses_`` id 并经 ``-s`` 注入。

背景：Corral 空白新建 / 接力新建原来起的是 ``opencode --auto [--prompt …]``，
没有任何会话 id；tmux 名又是随机 8 位 ``uuid``，SessKit 只能靠
``started <= created + 2s`` 时间猜把进程和 DB 行绑到一起（空窗误报
``Running in another window``，同 cwd 误认领串台）。``opencode -s <id>`` 的
语义是“continue, or create if it does not exist”（调用方给定的 id 被沿用，
不存在则建出该精确 id），校验仅 ``startsWith("ses")``，因此 launch 侧直接
预定 id 即可把绑定从“猜”变成“预知”，复用 SessKit 已有的 cmdline ``-s``
精确绑定通道，SessKit 零改动。

本模块只做三件事，不读会话正文、不碰 SessKit 解析、不改落盘历史：

1. ``new_session_id``：生成合法 ``ses_`` 串（前 8 字符嵌入短 ident 便于追溯，
   后 18 字符随机保证唯一）；
2. ``bind_hosted_ident``：TUI 新建类 argv 才注入 ``-s``（原生恢复已带 ``-s``、
   ``run``/``session``/``serve`` 等子命令一律不动），并把短 ident → 完整 id
   的映射写进 claim 文件，供占位卡精确转正（DB 行尚未落盘时保持 pending，
   绝不退回 cwd/mtime 猜测）；
3. claim 读写：``ses_id_for_ident`` / ``discard`` / ``prune_stale``，
   文件损坏、版本不符一律当 miss，``CORRAL_CACHE=0`` 时关闭读写。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from corral.models import LaunchPlan

SESSION_ID_ENV = "CORRAL_OPENCODE_SESSION_ID"
CLAIM_DIR = Path.home() / ".cache" / "corral" / "opencode-claims"
_SES_RE = re.compile(r"^ses_[A-Za-z0-9]+$")
_CLAIM_VERSION = 1
# 没有 provisional 与之对应的孤儿 claim（例如 wrap 后直接 exec 的直启路径）
# 最多保留 7 天，写入时顺手清理，避免 uuid 文件无限堆积。
_CLAIM_TTL_SECONDS = 7 * 24 * 3600


def _cache_disabled() -> bool:
    from corral.legacy_names import getenv

    return (getenv("CACHE", "1") or "1").strip().lower() in {
        "0", "false", "no", "off",
    }


def is_full_session_id(text: str) -> bool:
    """Corral 自生成完整会话 id 校验：``ses_`` + base62。

    比 SessKit 更严（SessKit ``_session_id_from_cmdline`` 只切 flag、
    ``_session_for_host_ident`` 只认 ``len>=16`` 前缀，见
    ``sesskit/parsers/opencode.py``）：这里只用于自生成 id 与 claim 校验，
    手造/损坏 claim 判 miss 后走各自降级，不影响扫描。
    """
    return bool(text) and bool(_SES_RE.match(str(text).strip()))


def new_session_id(short_ident: str = "") -> str:
    """生成预钉的完整会话 id：``ses_`` + 短 ident 前缀 + 随机后缀。

    后缀取足 26 字符（与默认生成器同长度，前 8 字符即 tmux 短 ident，
    便于日志追溯；随机部分 128 bit，碰撞可忽略）。
    """
    short = re.sub(r"[^0-9a-fA-F]", "", str(short_ident or ""))[:8].lower()
    rand = uuid.uuid4().hex + uuid.uuid4().hex
    suffix = (short + rand)[:26]
    return f"ses_{suffix}"


# 与 SessKit 同步（``sesskit/parsers/opencode.py`` ``_NON_TUI_SUBCOMMANDS``）：
# 任一表认为是子命令就不预钉（保守并集：往子命令里注 ``-s`` 是用法错误，
# 而漏钉一次只退回 pid 精确绑定）。两表任一增删都要同步另一处。
# SessKit 表（含 v1 遗留）：completion/acp/mcp/attach/run/debug/providers/
# agent/upgrade/uninstall/serve/web/models/stats/export/import/github/pr/
# session/plugin/db；Corral 表（``runtime/opencode.py`` ``SUBCOMMANDS``，v2
# ``--help`` 实测）另有 update/api/auth/mini/service/reload/pair。
_SESSKIT_NON_TUI_SUBCOMMANDS = frozenset(
    {
        "completion", "acp", "mcp", "attach", "run", "debug", "providers",
        "agent", "upgrade", "uninstall", "serve", "web", "models", "stats",
        "export", "import", "github", "pr", "session", "plugin", "db",
    }
)
# 与 SessKit 同步（``sesskit/parsers/opencode.py`` ``_VALUE_FLAGS``）：
# 扫描子命令时这些 flag 的下一个 token 是值，不得误判成子命令。
# 必须与 SessKit 用完全相同的集合（不是超集）：两边同错则行为一致，
# 单边多跳才会分叉（Corral 钉了而 SessKit 按非 TUI 忽略，或反之）。
_VALUE_FLAGS = frozenset(
    {
        "-s", "--session", "-m", "--model", "--agent", "--port",
        "--hostname", "--prompt", "--log-level", "--cors",
        "--mdns-domain",
    }
)


def _prompt_index(argv: list[str]) -> int | None:
    for index, token in enumerate(argv):
        if token == "--prompt" or token.startswith("--prompt="):
            return index
    return None


def _head_has_subcommand(head: list[str]) -> bool:
    """TUI 新建类 argv 才能预钉；子命令路径（run/session/serve/…）一律不动。

    子命令表取 Corral ``runtime/opencode.py`` 与 SessKit ``_NON_TUI_SUBCOMMANDS``
    的并集（任一认为是子命令就不钉）；flag 值跳过规则与 SessKit
    ``_VALUE_FLAGS`` 完全一致（两边同错则行为一致，单边分叉才会一钉一不钉）。
    """
    try:
        from corral.runtime.opencode import OpenCodeRuntime

        subcommands = set(OpenCodeRuntime.SUBCOMMANDS) | _SESSKIT_NON_TUI_SUBCOMMANDS
    except Exception:  # noqa: BLE001 — 导入失败时用保守的内置并集
        subcommands = {
            "upgrade", "update", "uninstall", "acp", "api", "debug", "auth",
            "mcp", "plugin", "models", "stats", "mini", "run", "session",
            "service", "reload", "pair", "serve",
        } | _SESSKIT_NON_TUI_SUBCOMMANDS
    skip_next = False
    for token in head:
        if skip_next:
            skip_next = False
            continue
        if token in _VALUE_FLAGS:
            skip_next = True
            continue
        if token.startswith("--session=") or token.startswith("--prompt="):
            continue
        if token.startswith("-"):
            continue
        if token in subcommands:
            return True
    return False


def _argv_has_session_flag(parts: list[str]) -> bool:
    """整条 argv（prompt 前后都算）是否已带会话 flag。

    token 精确比对：接力 ``--prompt`` 文本是单个 argv 元素，其内部的
    ``-s ses_…`` 字样不会精确命中；只有真正的重复 flag（原生恢复、
    ``run -s``、透传残留）才会拦下，避免注出双 ``-s``（opencode 按版本
    取后者或直接用法错误，不同版本行为不一）。
    """
    for token in parts:
        if token.startswith("--session="):
            return True
        if token in ("-s", "--session"):
            return True
    return False


def should_bind_plan(argv: tuple[str, ...] | list[str]) -> bool:
    """该 argv 是否为可预钉的 OpenCode TUI 新建（空白新建 / 接力新建）。"""
    if not argv:
        return False
    if os.path.basename(str(argv[0])) != "opencode":
        return False
    parts = [str(token) for token in argv[1:]]
    # prompt 后的 ``-s`` 也算已带：双 ``-s`` 在不同 opencode 版本行为不一
    # （取后者 / 用法错误），一律不钉，交原 argv 自己跑。
    if _argv_has_session_flag(parts):
        return False
    prompt_at = _prompt_index(parts)
    head = parts[:prompt_at] if prompt_at is not None else parts
    if _head_has_subcommand(head):
        return False
    return True


def bind_hosted_ident(
    plan: LaunchPlan, ident: str,
) -> tuple[LaunchPlan, list[str]]:
    """给 TUI 新建类计划钉上完整会话 id，返回（新计划，tmux -e 注入对）。

    非 TUI 路径（已带 ``-s`` 的原生恢复、子命令透传、非 opencode 可执行文件）
    原样返回，不写 claim。调用方（``keepalive.hosted_identity``）把返回的
    ``-e`` 对拼进 ``tmux new-session`` 即可；SessKit 随后经 cmdline ``-s``
    与进程 env 双通道精确绑定，无需改 SessKit。

    ``ident`` 必须是每次 launch 新生成的短 id（``keepalive.new_session_ident``
    同款 uuid 前缀）；同 ident 短时间内重复 launch 会复用已写入的新鲜 claim，
    避免同 pane 双 id（argv 与 claim 各拿一个，claim 永久 miss）。
    """
    if not should_bind_plan(list(plan.argv)):
        return plan, []
    short = str(ident or "")
    full_id = _reuse_fresh_claim(short)
    if full_id is None:
        full_id = new_session_id(short)
        write_intent(short, full_id, str(plan.cwd or ""))
    argv = [str(token) for token in plan.argv]
    # ``-s`` 必须落在 ``--prompt`` 之前：SessKit 只看 prompt 之前的旗标
    #（接力说明里常出现原会话的 ``-s ses_…`` 字样，整行扫会错绑）。
    prompt_at = _prompt_index(argv)
    insert_at = prompt_at if prompt_at is not None else len(argv)
    argv[insert_at:insert_at] = ["-s", full_id]
    return LaunchPlan(tuple(argv), plan.cwd), ["-e", f"{SESSION_ID_ENV}={full_id}"]


def _reuse_fresh_claim(short_ident: str, max_age_seconds: float = 300.0) -> str | None:
    """同 ident 短时间内重复 launch 时复用已写入的 claim，避免同 pane 双 id。"""
    try:
        path = _claim_path(short_ident)
        if path is None or not path.exists():
            return None
        if time.time() - path.stat().st_mtime > max_age_seconds:
            return None
    except OSError:
        return None
    return ses_id_for_ident(short_ident)


def _claim_path(short_ident: str) -> Path | None:
    text = str(short_ident or "").strip()
    if not text or "/" in text or text in {".", ".."}:
        return None
    return CLAIM_DIR / f"{text}.json"


def write_intent(short_ident: str, ses_id: str, cwd: str) -> bool:
    """记录短 ident → 完整会话 id 的创建意图；失败静默（调用方照常启动）。"""
    if _cache_disabled():
        return False
    if not short_ident or not is_full_session_id(ses_id):
        return False
    path = _claim_path(short_ident)
    if path is None:
        return False
    try:
        CLAIM_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "v": _CLAIM_VERSION,
            "ses_id": ses_id,
            "runtime": "opencode",
            "ident": str(short_ident),
            "cwd": str(cwd or ""),
            "created_at": time.time(),
        }
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".tmp", dir=CLAIM_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
            _fsync_dir(CLAIM_DIR)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        prune_stale()
        return True
    except OSError:
        return False


def _fsync_dir(directory: Path) -> None:
    """目录项落盘（掉电不丢 rename）；失败静默，调用方照常返回写入成功。"""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def ses_id_for_ident(short_ident: str) -> str | None:
    """该短 ident 预钉的完整会话 id；无 claim / 损坏 / 禁缓存时返回 None。"""
    if _cache_disabled():
        return None
    path = _claim_path(short_ident)
    if path is None:
        return None
    try:
        # 脏文件（>4KB 不可能是本模块写出的 ~150B claim）直接 miss，
        # 避免每 tick 全量 json.loads。
        if path.stat().st_size > 4096:
            return None
    except OSError:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("v") != _CLAIM_VERSION:
        return None
    ses_id = str(data.get("ses_id") or "")
    if not is_full_session_id(ses_id):
        return None
    if str(data.get("runtime") or "opencode") != "opencode":
        return None
    return ses_id


def discard(short_ident: str) -> None:
    """pane 退役/死亡时删掉它的 claim；失败静默。"""
    path = _claim_path(short_ident)
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def prune_stale(max_age_seconds: float = _CLAIM_TTL_SECONDS) -> int:
    """清掉超期孤儿 claim（直启 exec 等从未登记 provisional 的路径），返回清理数。

    兼清 crash 残留的 ``.*.tmp``（``write_intent`` 在 ``replace`` 前崩溃会留下
    无 ``.json`` 后缀的 dotfile，只增不减）。
    """
    if _cache_disabled():
        return 0
    now = time.time()
    pruned = 0
    try:
        files = list(CLAIM_DIR.glob("*.json")) + list(CLAIM_DIR.glob(".*.tmp"))
    except OSError:
        return 0
    for path in files:
        try:
            if now - path.stat().st_mtime > max_age_seconds:
                path.unlink()
                pruned += 1
        except OSError:
            continue
    return pruned
