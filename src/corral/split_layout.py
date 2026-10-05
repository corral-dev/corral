"""侧边栏记忆：会话组、置顶、折叠、上次焦点与侧边栏显隐。

持久化到 `~/.cache/corral/sidebar-layout.sqlite3`（多进程安全，WAL + 短事务）。

**这份记忆必须是「拿锁重读最新 → 应用本次改动 → 原子写回」，禁止退回「启动读一次、
之后整份覆盖」。** 早期版本把它存成 `split-layout.json` / `ui-prefs.json`，每个窗口启动时读
一次进内存，之后任何改动都整份覆盖写：同时开两个 corral 窗口时，后动手的窗口会把先动手窗口
的改动整份抹掉（丢的不是一条，而是全部置顶 + 全部分组），两个窗口也永远看不到对方的改动。
现在每次写都在 `BEGIN IMMEDIATE` 里重新读一遍最新状态再叠加，跨进程由 SQLite 保证互斥；
`layout_meta.revision` 每次写事务自增，界面据此感知别的窗口改了东西。

会话结束后仍保留分组；启动恢复时只重新打开仍在运行的成员。库不可用（只读盘、损坏）时降级为
进程内内存状态，界面照常工作，只是本次不落盘。
"""

from __future__ import annotations

import json
import logging
import os
import random
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from corral.legacy_names import cache_dir as product_cache_dir
from corral.legacy_names import env_is_set
from corral.models import parse_session_key
from corral.projects import normalize_cwd
from corral.titles import CACHE_DIR

LAYOUT_VERSION = 2
_SCHEMA_VERSION = 1
MAX_PANES = 4

logger = logging.getLogger(__name__)

# 水果名 → emoji：只收录 Unicode 有专属单字符 emoji 的水果（终端渲染稳定，不用 ZWJ
# 组合序列，避免旧字体/终端把 Lime 一类新版组合 emoji 拆成两个字符导致错位）。
_FRUIT_EMOJI = {
    "Apple": "🍎",
    "Avocado": "🥑",
    "Banana": "🍌",
    "Blueberry": "🫐",
    "Cherry": "🍒",
    "Coconut": "🥥",
    "Grape": "🍇",
    "Kiwi": "🥝",
    "Lemon": "🍋",
    "Mango": "🥭",
    "Melon": "🍈",
    "Orange": "🍊",
    "Peach": "🍑",
    "Pear": "🍐",
    "Pineapple": "🍍",
    "Strawberry": "🍓",
    "Watermelon": "🍉",
}
_FRUIT_NAMES = tuple(_FRUIT_EMOJI)


# 用户给分屏起名的长度上限（侧栏一行放不下也会截断，这里只防异常长输入落盘）。
MAX_GROUP_NAME_LENGTH = 80
GROUP_TITLE_SEPARATOR = " + "


def group_emoji(name: str) -> str:
    """从 `Group <Fruit>`／`Group <Fruit> <n>` 组名中取对应水果 emoji；取不到则返回空串。"""
    if not name.startswith("Group "):
        return ""
    fruit = name[len("Group ") :].split(" ", 1)[0]
    return _FRUIT_EMOJI.get(fruit, "")


def is_auto_group_name(name: str) -> bool:
    """内部水果身份名（`Group <Fruit>` / `Group <Fruit> N`）或空名视为「未命名」。

    这类名字只作落盘身份，不展示；其余名字是用户起的，原样展示。
    """
    if not name:
        return True
    if not name.startswith("Group "):
        return False
    parts = name[len("Group ") :].split(" ")
    if parts[0] not in _FRUIT_EMOJI:
        return False
    return len(parts) == 1 or (len(parts) == 2 and parts[1].isdigit())


def group_display_name(name: str, member_titles: Iterable[str]) -> str:
    """分屏显示名：用户起的名字原样返回；未命名时按分屏格顺序拼成员标题。"""
    if not is_auto_group_name(name):
        return name
    titles = [" ".join(str(title).split()) for title in member_titles]
    return GROUP_TITLE_SEPARATOR.join(title for title in titles if title)


def custom_group_name(name: str) -> str:
    """用户起的名字；未命名（水果身份名）返回空串。筛选只匹配这一个。"""
    return "" if is_auto_group_name(name) else name


def layout_cache_dir() -> Path:
    """与 `attention` / `cache` / `cursor_observer` 同一套约定（CORRAL_CACHE_DIR > XDG > ~/.cache）。"""
    return product_cache_dir()


def layout_db_path() -> Path:
    return layout_cache_dir() / "sidebar-layout.sqlite3"


def _legacy_dirs() -> list[str]:
    """一次性迁移要去哪些目录找旧版 JSON。

    **设了 `CORRAL_CACHE_DIR` 时只找该目录**，绝不回落到真实家目录：测试和临时验证
    全靠这个变量隔离，回落会让它们把机主真实的历史记忆一起搬走（真出过：一次验证脚本
    把本机 `ui-prefs.json` 迁进了临时库）。没设覆盖时才补上 `titles.CACHE_DIR`——
    旧代码不认环境变量，只往那儿写，设了 `XDG_CACHE_HOME` 的机器要靠这条才找得到。
    """
    dirs = [str(layout_cache_dir())]
    if not env_is_set("CACHE_DIR") and CACHE_DIR not in dirs:
        dirs.append(CACHE_DIR)
    return dirs


def _find_legacy(name: str) -> str | None:
    for directory in _legacy_dirs():
        path = os.path.join(directory, name)
        if os.path.exists(path):
            return path
    return None


def _timestamp(value: object) -> float:
    """把持久化时间转成可排序数值；损坏值按最旧处理。"""
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


@dataclass
class SplitGroup:
    """同一项目下的一组会话；成员结束后仍保留关系。"""

    group_id: str
    project_cwd: str
    session_keys: list[str]
    focus_key: str | None = None
    name: str = ""
    collapsed: bool = False
    updated_at: float = 0.0


@dataclass
class SplitLayoutStore:
    """侧边栏记忆的内存快照。

    这是一份**只读投影 + 纯变更逻辑**：界面直接拿它渲染，写盘一律经 `SidebarLayoutDB`，
    由后者在事务里对着最新快照重放这些方法。`revision` 是写盘时的版本号，界面靠它判断
    别的窗口有没有改过东西。
    """

    version: int = LAYOUT_VERSION
    last_project: str = ""
    last_focus_key: str = ""
    groups: dict[str, SplitGroup] = field(default_factory=dict)
    session_to_group: dict[str, str] = field(default_factory=dict)
    pinned_session_keys: dict[str, float] = field(default_factory=dict)
    pinned_group_ids: dict[str, float] = field(default_factory=dict)
    derived_group_pins: set[str] = field(default_factory=set, repr=False)
    revision: int = 0

    def adopt(self, other: SplitLayoutStore) -> None:
        """就地换成另一份快照的内容。

        必须就地更新而不是换实例：`SessionListView.group_store` 持有的是同一个引用，
        换实例会让侧边栏一直渲染旧对象。
        """
        if other is self:
            return
        self.version = other.version
        self.last_project = other.last_project
        self.last_focus_key = other.last_focus_key
        self.groups = other.groups
        self.session_to_group = other.session_to_group
        self.pinned_session_keys = other.pinned_session_keys
        self.pinned_group_ids = other.pinned_group_ids
        self.derived_group_pins = set(other.derived_group_pins)
        self.revision = other.revision

    def get_group(self, session_key: str) -> SplitGroup | None:
        gid = self.session_to_group.get(session_key)
        if not gid:
            return None
        return self.groups.get(gid)

    def group_session_keys(self, session_key: str) -> list[str]:
        group = self.get_group(session_key)
        if group is None:
            return [session_key]
        return list(group.session_keys)

    def ordered_groups(self) -> list[SplitGroup]:
        """按最近创建或使用时间返回会话组，最新的组排在侧边栏最上方。"""
        return sorted(
            self.groups.values(),
            key=lambda group: (group.updated_at, group.group_id),
            reverse=True,
        )

    def set_collapsed(self, group_id: str, collapsed: bool) -> bool:
        """更新侧边栏折叠态；实际变化时返回 True。"""
        group = self.groups.get(group_id)
        if group is None or group.collapsed == collapsed:
            return False
        group.collapsed = collapsed
        return True

    def toggle_session_pin(self, session_key: str) -> bool:
        """切换独立会话置顶状态，返回切换后的状态。

        本方法只翻独立键，不在这里提升整组——提升统一由 ``_normalize_store``
        在读/写路径收敛（桌面侧栏默认提升；移动端经
        ``SidebarLayoutDB.toggle_session_pin(..., promote_to_group=False)`` /
        ``read_with_promote(skip_promote=True)`` 跳过提升）。
        """
        if session_key in self.pinned_session_keys:
            del self.pinned_session_keys[session_key]
            return False
        self.pinned_session_keys[session_key] = time.time()
        return True

    def toggle_group_pin(self, group_id: str) -> bool:
        """切换整个会话组置顶状态，返回切换后的状态。

        取消整组置顶时必须同时清掉成员的独立 pin。`_mutate` / `_read_conn` 每次都会跑
        `_normalize_store` → `_promote_member_pins_to_group`：进组保留独立键是为了筛选
        解散后还能回到 pinned，但如果 unpin 只删 `pinned_group_ids`，成员键还在就会立刻
        把组钉回去，界面弹出 Pinned / 已置顶，分组却取消不了。
        """
        if group_id in self.pinned_group_ids:
            del self.pinned_group_ids[group_id]
            self.derived_group_pins.discard(group_id)
            group = self.groups.get(group_id)
            if group is not None:
                for key in group.session_keys:
                    self.pinned_session_keys.pop(key, None)
            return False
        if group_id not in self.groups:
            return False
        self.pinned_group_ids[group_id] = time.time()
        return True

    def set_group(
        self,
        project_cwd: str,
        session_keys: list[str],
        *,
        focus_key: str | None = None,
    ) -> None:
        """写入或更新组合；session_keys 去重保序，最多 MAX_PANES 个。"""
        keys: list[str] = []
        seen: set[str] = set()
        for key in session_keys:
            if key and key not in seen:
                keys.append(key)
                seen.add(key)
        if len(keys) < 2:
            return
        if len(keys) > MAX_PANES:
            keys = keys[:MAX_PANES]
        focus = focus_key if focus_key in keys else keys[0]
        # 改动现有分屏（增格、换成员）时沿用原组名；全新组合才生成新身份。
        existing_gid = next(
            (self.session_to_group[key] for key in keys if key in self.session_to_group),
            None,
        )
        existing = self.groups.get(existing_gid) if existing_gid else None
        self._drop_sessions_from_other_groups(keys, keep_gid=existing_gid)
        gid = existing_gid or str(uuid.uuid4())
        if existing is None:
            existing = SplitGroup(
                group_id=gid,
                project_cwd=project_cwd,
                session_keys=keys,
                focus_key=focus,
                name=self._new_group_name(),
            )
            self.groups[gid] = existing
        else:
            existing.project_cwd = project_cwd
            existing.session_keys = keys
            existing.focus_key = focus
            if not existing.name:
                existing.name = self._new_group_name()
        existing.updated_at = time.time()
        self.last_project = project_cwd
        self.last_focus_key = focus or ""
        self._reindex_group(gid)
        self._promote_member_pins_to_group(gid)

    def set_focus(self, project_cwd: str, session_key: str) -> None:
        """只记录当前焦点，不新建、不复活、不重排会话组。

        右栏切格必须走这条而不是 `set_group()`：后者会把当前右栏组合整份重新断言一遍，
        另一个窗口刚把某成员移出去时，这边一切焦点就又把组重建回来（组名还会重新随机），
        两个窗口来回打架。
        """
        if not session_key:
            return
        group = self.get_group(session_key)
        if group is not None:
            group.focus_key = session_key
            self.last_project = group.project_cwd or project_cwd
        else:
            self.last_project = project_cwd
        self.last_focus_key = session_key

    def migrate_session_key(self, old_key: str, new_key: str) -> None:
        """占位卡转正或重扫后会话键变化时，把分屏记忆从旧键迁到新键。"""
        if not old_key or not new_key or old_key == new_key:
            return
        pinned_at = self.pinned_session_keys.pop(old_key, None)
        if pinned_at is not None:
            self.pinned_session_keys[new_key] = pinned_at
        gid = self.session_to_group.get(old_key)
        if not gid:
            return
        group = self.groups.get(gid)
        if group is None:
            return
        seen: set[str] = set()
        migrated: list[str] = []
        for key in group.session_keys:
            mapped = new_key if key == old_key else key
            if mapped not in seen:
                migrated.append(mapped)
                seen.add(mapped)
        group.session_keys = migrated[:MAX_PANES]
        if group.focus_key == old_key:
            group.focus_key = new_key
        if self.last_focus_key == old_key:
            self.last_focus_key = new_key
        if len(group.session_keys) < 2:
            self._delete_group(gid)
            return
        self._drop_sessions_from_other_groups(group.session_keys)
        self._reindex_group(gid)

    def remove_session(self, session_key: str) -> None:
        """从组合中移除单个会话；不足两个成员时解散组。"""
        self.pinned_session_keys.pop(session_key, None)
        gid = self.session_to_group.pop(session_key, None)
        if not gid:
            return
        group = self.groups.get(gid)
        if group is None:
            return
        group.session_keys = [k for k in group.session_keys if k != session_key]
        if len(group.session_keys) < 2:
            self._delete_group(gid)
            return
        if group.focus_key == session_key:
            group.focus_key = group.session_keys[0]
        group.updated_at = time.time()
        self._reindex_group(gid)

    def _drop_sessions_from_other_groups(
        self, keys: list[str], *, keep_gid: str | None = None,
    ) -> None:
        """新组合写入前，把这些键从旧组摘掉（避免一键多组）。"""
        for key in keys:
            old_gid = self.session_to_group.get(key)
            if not old_gid or old_gid == keep_gid:
                continue
            group = self.groups.get(old_gid)
            if group is None:
                continue
            if set(group.session_keys) == set(keys):
                continue
            group.session_keys = [k for k in group.session_keys if k != key]
            if len(group.session_keys) < 2:
                self._delete_group(old_gid)
            else:
                if group.focus_key == key:
                    group.focus_key = group.session_keys[0]
                self._reindex_group(old_gid)

    def _delete_group(self, gid: str) -> None:
        """删除组及其全部反向索引，不删除任何会话。"""
        self.groups.pop(gid, None)
        self.pinned_group_ids.pop(gid, None)
        self.derived_group_pins.discard(gid)
        for key in list(self.session_to_group):
            if self.session_to_group.get(key) == gid:
                del self.session_to_group[key]

    def rename_group(self, group_id: str, name: str) -> bool:
        """用户给分屏起名；空名恢复自动名（换回一个内部水果身份名）。实际变化时返回 True。"""
        group = self.groups.get(group_id)
        if group is None:
            return False
        cleaned = " ".join(str(name or "").split())[:MAX_GROUP_NAME_LENGTH]
        if not cleaned:
            if is_auto_group_name(group.name):
                return False
            group.name = ""
            group.name = self._new_group_name()
            return True
        if cleaned == group.name:
            return False
        group.name = cleaned
        return True

    def _new_group_name(self) -> str:
        """生成当前布局内不重名的水果组名；水果名用尽后随机挑一种水果加序号。"""
        used = {group.name for group in self.groups.values() if group.name}
        available = [
            f"Group {fruit}"
            for fruit in _FRUIT_NAMES
            if f"Group {fruit}" not in used
        ]
        if available:
            return random.SystemRandom().choice(available)
        # 17 种水果全被历史组占用：随机挑一种水果配最小空闲序号，保住 emoji 辨识度
        # 与随机性，而不是所有新组都挤在 "Group Apple N" 上。带序号的兜底不用
        # Apple——那正是旧版遗留名 "Group Apple N" 的形状，会被迁移逻辑误判再改名。
        fruits = tuple(fruit for fruit in _FRUIT_NAMES if fruit != "Apple")
        return self._indexed_group_name(random.SystemRandom().choice(fruits), used)

    def _indexed_group_name(self, fruit: str, used: set[str]) -> str:
        """给定水果，返回带最小空闲序号的组名（"Group <水果> N"）。"""
        assert fruit != "Apple", "带序号兑底名禁止 Apple，避免撞旧版遗留名形状"
        index = 2
        while f"Group {fruit} {index}" in used:
            index += 1
        return f"Group {fruit} {index}"

    def _reindex_group(self, gid: str) -> None:
        group = self.groups.get(gid)
        if group is None:
            return
        for key in list(self.session_to_group):
            if self.session_to_group.get(key) == gid and key not in group.session_keys:
                del self.session_to_group[key]
        for key in group.session_keys:
            self.session_to_group[key] = gid

    def _promote_member_pins_to_group(self, gid: str) -> None:
        """成员上的独立置顶在组可见时提升为整组置顶（纯桌面展示派生）。

        落盘只保留独立键与桌面显式组钉；这里算出来的组钉记进
        ``derived_group_pins``，写盘时过滤掉，别的桌面写操作不会把它固化成
        显式组意图（否则手机解钉后桌面会留下幽灵组钉）。

        进组不再毁掉独立置顶：组后来不足两名可见成员时（筛选、对端电脑的
        另一成员没被扫到），独立置顶还能回到 pinned 区。组正在展示时用组
        置顶，避免同一会话既在组里又单独钉一份。用户明确取消整组置顶时由
        `toggle_group_pin` 先清成员键，再走到这里才不会被立刻钉回去。
        """
        group = self.groups.get(gid)
        if group is None or gid in self.pinned_group_ids:
            return
        times = [
            self.pinned_session_keys[key]
            for key in group.session_keys
            if key in self.pinned_session_keys
        ]
        if times:
            self.pinned_group_ids[gid] = max(times)
            self.derived_group_pins.add(gid)


def sidebar_fingerprint(store: SplitLayoutStore) -> tuple:
    """侧边栏**看得见**的那部分状态；焦点类字段不计入。

    界面据此决定要不要重建列表：全量重建是秒级重活（见
    `docs/TERMINAL_UI_KNOWLEDGE_BASE.md` 的并发重建教训），只切焦点不该触发。
    """
    return (
        tuple(
            (
                gid,
                group.name,
                group.collapsed,
                tuple(group.session_keys),
                store.pinned_group_ids.get(gid),
            )
            for gid, group in sorted(store.groups.items())
        ),
        tuple(sorted(store.pinned_session_keys.items())),
    )


def _persisted_state(store: SplitLayoutStore) -> tuple:
    """写盘内容的比对指纹（含焦点字段），用于判断本次改动是否真的改了东西。"""
    return (
        store.last_project,
        store.last_focus_key,
        tuple(
            (gid, group.project_cwd, group.focus_key, group.updated_at)
            for gid, group in sorted(store.groups.items())
        ),
        sidebar_fingerprint(store),
    )


class SidebarLayoutDB:
    """多进程安全的侧边栏记忆库。

    所有写操作都是「事务内重读最新 → 重放这一次改动 → 整表写回 + revision 自增」。
    数据量只有几个组和几条置顶，整表写回比按行 diff 简单得多，正确性由事务保证。
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path).expanduser() if path is not None else None
        self._lock = threading.RLock()
        self._warning_lock = threading.Lock()
        self._degraded_reported = False
        # 库打不开时的进程内兜底状态：界面照常工作，只是本次不落盘。
        self._memory: SplitLayoutStore | None = None
        self._memory_sidebar_visible: bool | None = None
        # 常驻连接：读写都持 self._lock，跨线程只在这个实例内部串行使用。
        # 之前每次读写都 connect+PRAGMA+建表+迁移探测再 close，单次写实测 8~18ms
        # 且全压在界面线程上；read_revision 还是每秒轮询路径，一直在白付这笔钱。
        self._conn: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        # 路径惰性解析：测试会在建好对象之后再改环境变量或打桩缓存目录。
        return self._path if self._path is not None else layout_db_path()

    def _report_degraded(self, error: BaseException) -> None:
        with self._warning_lock:
            if self._degraded_reported:
                return
            self._degraded_reported = True
        logger.warning("侧边栏记忆不可用，本次改动不会保存：%s", error)

    def _open(self) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        conn: sqlite3.Connection | None = None
        try:
            path = self.path
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            conn = sqlite3.connect(path, timeout=1.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=1000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._init_schema(conn)
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            self._import_legacy(conn)
            self._conn = conn
            return conn
        except (OSError, sqlite3.Error) as error:
            if conn is not None:
                conn.close()
            self._report_degraded(error)
            return None

    def close(self) -> None:
        """Release the cached connection so tests can unlink the sqlite file."""
        with self._lock:
            conn = self._conn
            self._conn = None
        if conn is not None:
            self._discard_conn(conn)

    def _discard_conn(self, conn: sqlite3.Connection) -> None:
        """连接出错时丢弃缓存，下次调用自动重开（自愈，不中断界面）。"""
        if self._conn is conn:
            self._conn = None
        try:
            conn.close()
        except sqlite3.Error:
            pass

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS layout_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS split_group (
                group_id TEXT PRIMARY KEY,
                project_cwd TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                focus_key TEXT,
                collapsed INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0,
                pinned_at REAL
            );
            CREATE TABLE IF NOT EXISTS split_group_member (
                group_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                session_key TEXT NOT NULL,
                PRIMARY KEY(group_id, position)
            );
            CREATE INDEX IF NOT EXISTS split_group_member_key
            ON split_group_member(session_key);
            CREATE TABLE IF NOT EXISTS pinned_session (
                session_key TEXT PRIMARY KEY,
                pinned_at REAL NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO layout_meta(key, value) VALUES('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        conn.execute(
            "INSERT OR IGNORE INTO layout_meta(key, value) VALUES('revision', '0')"
        )
        conn.commit()

    # ---- 元信息 ----

    @staticmethod
    def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute(
            "SELECT value FROM layout_meta WHERE key=?", (key,),
        ).fetchone()
        return None if row is None else str(row[0])

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO layout_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---- 一次性迁移 ----

    def _import_legacy(self, conn: sqlite3.Connection) -> None:
        """把旧版两个 JSON 文件导入一次。

        幂等：`imported_legacy` 置位后不再执行；旧文件缺失也照样置位，避免每次开库都探一遍。
        **只读不动旧文件**：不改名、不删除。升级期间机主机器上很可能还开着运行旧代码的
        corral 窗口，它仍在按秒往那两个文件里写；把文件改名只会和它互相打架，还会在
        回退到旧版本时凭空丢掉记忆。留着当天然备份，代价只是两个几 KB 的孤儿文件。
        """
        if self._get_meta(conn, "imported_legacy") == "1":
            return
        store = None
        layout_file = _find_legacy("split-layout.json")
        if layout_file is not None:
            store = _load_legacy_layout(layout_file)
        visible = None
        prefs_file = _find_legacy("ui-prefs.json")
        if prefs_file is not None:
            visible = _load_legacy_sidebar_visible(prefs_file)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if self._get_meta(conn, "imported_legacy") == "1":
                conn.commit()
                return
            if store is not None:
                store.revision = 1
                self._write_conn(conn, store)
            if visible is not None:
                self._set_meta(conn, "sidebar_visible", "1" if visible else "0")
            self._set_meta(conn, "imported_legacy", "1")
            conn.commit()
        except (OSError, sqlite3.Error) as error:
            conn.rollback()
            self._report_degraded(error)

    # ---- 读 ----

    @staticmethod
    def _read_conn(conn: sqlite3.Connection, *, skip_promote: bool = False) -> SplitLayoutStore:
        store = SplitLayoutStore()
        try:
            store.revision = int(SidebarLayoutDB._get_meta(conn, "revision") or 0)
        except (TypeError, ValueError):
            store.revision = 0
        store.last_project = SidebarLayoutDB._get_meta(conn, "last_project") or ""
        store.last_focus_key = SidebarLayoutDB._get_meta(conn, "last_focus_key") or ""
        members: dict[str, list[str]] = {}
        for row in conn.execute(
            "SELECT group_id, session_key FROM split_group_member "
            "ORDER BY group_id, position"
        ):
            members.setdefault(str(row["group_id"]), []).append(str(row["session_key"]))
        for row in conn.execute(
            "SELECT group_id, project_cwd, name, focus_key, collapsed, updated_at, pinned_at "
            "FROM split_group"
        ):
            gid = str(row["group_id"])
            session_keys = members.get(gid, [])[:MAX_PANES]
            # 单成员组是残留，不还原（与旧版 load_layout 的判定一致）。
            if len(session_keys) < 2:
                continue
            store.groups[gid] = SplitGroup(
                group_id=gid,
                project_cwd=str(row["project_cwd"] or ""),
                session_keys=session_keys,
                focus_key=str(row["focus_key"]) if row["focus_key"] else None,
                name=str(row["name"] or ""),
                collapsed=bool(row["collapsed"]),
                updated_at=_timestamp(row["updated_at"]),
            )
            if row["pinned_at"] is not None:
                store.pinned_group_ids[gid] = _timestamp(row["pinned_at"])
        for row in conn.execute("SELECT session_key, pinned_at FROM pinned_session"):
            store.pinned_session_keys[str(row["session_key"])] = _timestamp(row["pinned_at"])
        _normalize_store(store, skip_promote=skip_promote)
        return store

    @staticmethod
    def _write_conn(conn: sqlite3.Connection, store: SplitLayoutStore) -> None:
        conn.execute("DELETE FROM split_group_member")
        conn.execute("DELETE FROM split_group")
        conn.execute("DELETE FROM pinned_session")
        for gid, group in store.groups.items():
            conn.execute(
                "INSERT INTO split_group"
                "(group_id, project_cwd, name, focus_key, collapsed, updated_at, pinned_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    gid,
                    group.project_cwd,
                    group.name,
                    group.focus_key,
                    1 if group.collapsed else 0,
                    group.updated_at,
                    None if gid in store.derived_group_pins else store.pinned_group_ids.get(gid),
                ),
            )
            for position, key in enumerate(group.session_keys):
                conn.execute(
                    "INSERT INTO split_group_member(group_id, position, session_key) "
                    "VALUES(?,?,?)",
                    (gid, position, key),
                )
        for key, pinned_at in store.pinned_session_keys.items():
            conn.execute(
                "INSERT INTO pinned_session(session_key, pinned_at) VALUES(?,?)",
                (key, pinned_at),
            )
        SidebarLayoutDB._set_meta(conn, "revision", str(store.revision))
        SidebarLayoutDB._set_meta(conn, "last_project", store.last_project)
        SidebarLayoutDB._set_meta(conn, "last_focus_key", store.last_focus_key)

    def _fallback(self) -> SplitLayoutStore:
        if self._memory is None:
            self._memory = SplitLayoutStore()
        return self._memory

    def read(self) -> SplitLayoutStore:
        """读一份最新快照；库不可用时返回进程内兜底状态。"""
        return self.read_with_promote(skip_promote=False)

    def read_with_promote(self, *, skip_promote: bool = False) -> SplitLayoutStore:
        """读一份最新快照，可选跳过成员独立钉到整组置顶的提升。

        移动端（没有分组概念）传 ``skip_promote=True``：只看独立会话置顶，
        读出来的 ``pinned_group_ids`` 只含桌面真正钉过的组（落盘 ``pinned_at``
        非空），不会把成员独立钉提升进来。桌面侧栏仍用默认 ``read()``。
        """
        with self._lock:
            conn = self._open()
            if conn is None:
                return self._fallback()
            try:
                return self._read_conn(conn, skip_promote=skip_promote)
            except (OSError, sqlite3.Error) as error:
                self._discard_conn(conn)
                self._report_degraded(error)
                return self._fallback()

    def read_revision(self) -> int:
        """只取版本号：界面每秒轮询用，别在这条路径上读整份快照。"""
        with self._lock:
            conn = self._open()
            if conn is None:
                return self._fallback().revision
            try:
                return int(self._get_meta(conn, "revision") or 0)
            except (OSError, sqlite3.Error, TypeError, ValueError) as error:
                self._discard_conn(conn)
                self._report_degraded(error)
                return self._fallback().revision

    # ---- 写 ----

    def _mutate(self, apply: Callable[[SplitLayoutStore], None], *, skip_promote: bool = False) -> SplitLayoutStore:
        """事务内重读最新状态，重放这一次改动，再整体写回。"""
        with self._lock:
            conn = self._open()
            if conn is None:
                store = self._fallback()
                apply(store)
                _normalize_store(store, skip_promote=skip_promote)
                store.revision += 1
                return store
            try:
                conn.execute("BEGIN IMMEDIATE")
                store = self._read_conn(conn, skip_promote=skip_promote)
                before = _persisted_state(store)
                apply(store)
                _normalize_store(store, skip_promote=skip_promote)
                if _persisted_state(store) != before:
                    store.revision += 1
                    self._write_conn(conn, store)
                conn.commit()
                return store
            except (OSError, sqlite3.Error) as error:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                self._discard_conn(conn)
                self._report_degraded(error)
                store = self._fallback()
                apply(store)
                _normalize_store(store, skip_promote=skip_promote)
                return store

    def apply(self, mutate: Callable[[SplitLayoutStore], object]) -> SplitLayoutStore:
        """在事务里对最新快照重放一次任意改动，返回改完后的最新快照。

        `mutate` 只能调用 `SplitLayoutStore` 上的纯变更方法，不要在里面读界面状态：
        它拿到的是库里刚读出来的快照，不是调用方手上那份。
        """
        return self._mutate(mutate)

    def set_group(
        self,
        project_cwd: str,
        session_keys: list[str],
        *,
        focus_key: str | None = None,
    ) -> SplitLayoutStore:
        keys = list(session_keys)
        return self._mutate(
            lambda store: store.set_group(project_cwd, keys, focus_key=focus_key)
        )

    def set_focus(self, project_cwd: str, session_key: str) -> SplitLayoutStore:
        return self._mutate(lambda store: store.set_focus(project_cwd, session_key))

    def remove_session(self, session_key: str) -> SplitLayoutStore:
        return self._mutate(lambda store: store.remove_session(session_key))

    def migrate_session_key(self, old_key: str, new_key: str) -> SplitLayoutStore:
        return self._mutate(lambda store: store.migrate_session_key(old_key, new_key))

    def set_collapsed(self, group_id: str, collapsed: bool) -> SplitLayoutStore:
        return self._mutate(lambda store: store.set_collapsed(group_id, collapsed))

    def toggle_session_pin(
        self, session_key: str, *, promote_to_group: bool = True
    ) -> SplitLayoutStore:
        """切换独立会话置顶。

        翻转依据是库里的最新状态，不是调用方手上那份快照——多窗口下这是唯一不会
        互相覆盖的语义。调用方从返回的快照里读切换结果。移动端（没有分组概念）
        传 ``promote_to_group=False``：pin 一条只钉那一条，不提升整组置顶。
        """
        skip = not promote_to_group
        return self._mutate(
            lambda store: store.toggle_session_pin(session_key),
            skip_promote=skip,
        )

    def toggle_group_pin(self, group_id: str) -> SplitLayoutStore:
        return self._mutate(lambda store: store.toggle_group_pin(group_id))

    def rename_group(self, group_id: str, name: str) -> SplitLayoutStore:
        return self._mutate(lambda store: store.rename_group(group_id, name))

    # ---- 侧边栏显隐（启动时套用的偏好，不参与跨窗口实时同步）----

    def sidebar_visible(self, *, default: bool = True) -> bool:
        with self._lock:
            conn = self._open()
            if conn is None:
                value = self._memory_sidebar_visible
                return default if value is None else value
            try:
                raw = self._get_meta(conn, "sidebar_visible")
                if raw is None:
                    return default
                return raw == "1"
            except (OSError, sqlite3.Error) as error:
                self._discard_conn(conn)
                self._report_degraded(error)
                return default

    def set_sidebar_visible(self, visible: bool) -> None:
        with self._lock:
            conn = self._open()
            if conn is None:
                self._memory_sidebar_visible = bool(visible)
                return
            try:
                self._set_meta(conn, "sidebar_visible", "1" if visible else "0")
                conn.commit()
            except (OSError, sqlite3.Error) as error:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                self._discard_conn(conn)
                self._report_degraded(error)


def _is_legacy_fallback_name(name: str) -> bool:
    """旧版兜底名 "Group Apple N"：水果池耗尽后全部挤在 Apple 上，读入时重生成。"""
    suffix = name[len("Group Apple "):] if name.startswith("Group Apple ") else ""
    return bool(suffix) and suffix.isdigit()


def _normalize_store(store: SplitLayoutStore, *, skip_promote: bool = False) -> None:
    """统一收敛快照里的派生约束：组名、反向索引、置顶与组的从属关系。

    ``skip_promote=True`` 时不提升成员独立钉为整组置顶——移动端 pin 单会话
    的写路径用：pin 一条只钉那一条，不得把同组其它会话一起钉上去。
    """
    # Derived desktop presentation must never become an explicit persisted pin.
    for gid in store.derived_group_pins:
        store.pinned_group_ids.pop(gid, None)
    store.derived_group_pins.clear()
    for gid in list(store.groups):
        if len(store.groups[gid].session_keys) < 2:
            store._delete_group(gid)
    # 迁移旧兜底名（"Group Apple N"）时用确定性分配：读路径不落盘，随机名会让
    # 每次读取重随一遍、多窗口名字对不上；新建组仍走 `_new_group_name` 的随机路径。
    rename = sorted(
        (
            group
            for group in store.groups.values()
            if not group.name or _is_legacy_fallback_name(group.name)
        ),
        key=lambda group: (group.updated_at, group.group_id),
    )
    used = {group.name for group in store.groups.values() if group.name}
    for salt, group in enumerate(rename):
        used.discard(group.name)
        candidates = _FRUIT_NAMES[salt % len(_FRUIT_NAMES):] + _FRUIT_NAMES[: salt % len(_FRUIT_NAMES)]
        name = next(
            (f"Group {fruit}" for fruit in candidates if f"Group {fruit}" not in used),
            None,
        )
        if name is None:
            # 纯水果名全被占用：带序号兑底不用 Apple，避免撞旧版遗留名形状。
            fruit = next(f for f in candidates if f != "Apple")
            name = store._indexed_group_name(fruit, used)
        group.name = name
        used.add(name)
    store.session_to_group.clear()
    for gid in store.groups:
        store._reindex_group(gid)
    store.pinned_group_ids = {
        gid: pinned_at
        for gid, pinned_at in store.pinned_group_ids.items()
        if gid in store.groups
    }
    for gid in store.groups:
        if not skip_promote:
            store._promote_member_pins_to_group(gid)


def _load_legacy_layout(path: str) -> SplitLayoutStore | None:
    """读旧版 split-layout.json；文件缺失或损坏返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    store = SplitLayoutStore()
    store.last_project = str(raw.get("last_project") or "")
    store.last_focus_key = str(raw.get("last_focus_key") or "")
    pinned_sessions = raw.get("pinned_session_keys") or {}
    if isinstance(pinned_sessions, dict):
        store.pinned_session_keys = {
            str(key): _timestamp(value)
            for key, value in pinned_sessions.items()
            if key
        }
    pinned_groups = raw.get("pinned_group_ids") or {}
    if isinstance(pinned_groups, dict):
        store.pinned_group_ids = {
            str(key): _timestamp(value)
            for key, value in pinned_groups.items()
            if key
        }
    groups_raw = raw.get("groups") or {}
    if isinstance(groups_raw, dict):
        for gid, g in groups_raw.items():
            if not isinstance(g, dict):
                continue
            keys = g.get("session_keys") or []
            if not isinstance(keys, list):
                continue
            session_keys = [str(k) for k in keys if k][:MAX_PANES]
            if len(session_keys) < 2:
                continue
            store.groups[str(gid)] = SplitGroup(
                group_id=str(gid),
                project_cwd=str(g.get("project_cwd") or ""),
                session_keys=session_keys,
                focus_key=str(g["focus_key"]) if g.get("focus_key") else None,
                name=str(g.get("name") or ""),
                collapsed=bool(g.get("collapsed", False)),
                updated_at=_timestamp(g.get("updated_at")),
            )
    _normalize_store(store)
    return store


def _load_legacy_sidebar_visible(path: str) -> bool | None:
    """读旧版 ui-prefs.json 的侧边栏显隐；缺失或损坏返回 None。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("sidebar_visible")
    return value if isinstance(value, bool) else None


_DEFAULT_DB: SidebarLayoutDB | None = None
_DEFAULT_DB_LOCK = threading.Lock()


def default_layout_db() -> SidebarLayoutDB:
    """进程内共用的库句柄（界面与壳层偏好共用同一份）。"""
    global _DEFAULT_DB
    with _DEFAULT_DB_LOCK:
        if _DEFAULT_DB is None:
            _DEFAULT_DB = SidebarLayoutDB()
        return _DEFAULT_DB


def pinned_keys_effective() -> set[str] | None:
    """Session keys the user pinned: individually, or as a member of a pinned group.

    Read-only view for the silent reclaimer, which must never stop a session the
    user explicitly pinned. Groups that are merely remembered but not pinned do
    not count. Returns ``None`` when the layout database is unreadable (the
    reader then hands back its in-process fallback, which knows no pins) so the
    caller treats pins as unknown instead of absent.
    """
    db = default_layout_db()
    store = db.read()
    if store is db._memory:
        return None
    keys = set(store.pinned_session_keys)
    for gid in store.pinned_group_ids:
        group = store.groups.get(gid)
        if group is not None:
            keys.update(group.session_keys)
    return keys


def remembered_ids_by_runtime() -> dict[str, set[str]]:
    """置顶与分组成员的会话 id，按运行时分组，供扫描 limit 豁免。"""
    store = default_layout_db().read()
    out: dict[str, set[str]] = {}
    keys = set(store.pinned_session_keys)
    for group in store.groups.values():
        keys.update(group.session_keys)
    for key in keys:
        runtime_id, session_id = parse_session_key(key)
        if runtime_id == "unknown" or not session_id:
            continue
        out.setdefault(runtime_id, set()).add(session_id)
    return out


def reset_default_layout_db() -> None:
    """丢弃进程内共用句柄，供测试切换缓存目录后重新解析路径。"""
    global _DEFAULT_DB
    with _DEFAULT_DB_LOCK:
        db = _DEFAULT_DB
        _DEFAULT_DB = None
    if db is not None:
        db.close()


def resolve_active_group(
    store: SplitLayoutStore,
    session_key: str,
    *,
    is_active: Callable[[str], bool],
    find_session: Callable[[str], dict | None],
) -> tuple[str, list[str]]:
    """解析选中会话应恢复的分屏组合。

    返回 (project_cwd, ordered_session_keys)；同伴已不活跃则降级为单格。
    """
    group = store.get_group(session_key)
    if group is None:
        session = find_session(session_key)
        project = ""
        if session:
            project = normalize_cwd(session.get("cwd"))
        return project, [session_key]
    alive = [k for k in group.session_keys if is_active(k)]
    if session_key not in alive:
        alive = [session_key] if is_active(session_key) else []
    if not alive:
        return group.project_cwd, [session_key]
    if session_key in alive and len(alive) == 1:
        return group.project_cwd, alive
    # 保持原顺序，只留活跃成员
    ordered = [k for k in group.session_keys if k in alive]
    if session_key in ordered:
        # 聚焦项不变，顺序保持
        pass
    elif session_key in alive:
        ordered = [session_key] + [k for k in ordered if k != session_key]
    return group.project_cwd, ordered or [session_key]
