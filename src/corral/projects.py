"""本机项目发现：会话 cwd ∪ 文件系统 git 根，供快捷启动与 TUI 新建共用。

扫描策略参考常见「家目录下找 git 根」做法，但不耦合其它产品的环境变量。
硬排除 `.stversions` 等目录，避免 Syncthing 版本快照里的 `.git` 冒充项目。
"""

from __future__ import annotations

import errno
import fnmatch
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TextIO

from corral.i18n import t
from corral.legacy_names import env_is_set, getenv
from corral.scan.common import is_ephemeral_agent_cwd


class _LocalizedLabel:
    """可当字符串用的惰性文案：比较/拼接时按当前语言求值。"""

    def __init__(self, key: str) -> None:
        self._key = key

    def __str__(self) -> str:
        from corral.i18n import t

        return t(self._key)

    def __eq__(self, other: object) -> bool:
        return str(self) == other

    def __hash__(self) -> int:
        return hash(self._key)

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)

    def __add__(self, other: object) -> str:
        return str(self) + str(other)

    def __radd__(self, other: object) -> str:
        return str(other) + str(self)


UNKNOWN_PROJECT_LABEL = _LocalizedLabel("project.unknown_dir")


def normalize_cwd(cwd: object) -> str:
    """把工作目录归一化为分组/过滤用的唯一键；空值或根目录归一为空字符串。"""
    text = str(cwd or "").strip()
    if not text:
        return ""
    normalized = os.path.normpath(text)
    if normalized in (".", "/"):
        return ""
    return normalized


def disambiguate_labels(cwd_keys: list[str]) -> dict[str, str]:
    """同名末级目录逐级向上补父级路径，直到唯一（VS Code 标签页风格）。"""
    parts = {key: [p for p in key.split("/") if p] for key in cwd_keys}
    depth = {key: 1 for key in cwd_keys}
    labels: dict[str, str] = {}

    while True:
        labels = {}
        for key in cwd_keys:
            segments = parts[key]
            d = min(depth[key], len(segments)) if segments else 0
            labels[key] = "/".join(segments[-d:]) if d else key

        groups: dict[str, list[str]] = {}
        for key, label in labels.items():
            groups.setdefault(label, []).append(key)

        changed = False
        for members in groups.values():
            if len(members) <= 1:
                continue
            for key in members:
                if depth[key] < len(parts[key]):
                    depth[key] += 1
                    changed = True
        if not changed:
            return labels


def fuzzy_match(query: str, *texts: str) -> bool:
    """大小写无关模糊匹配：子串包含，或查询字符按序出现（子序列）。

    空查询视为匹配全部。用于侧边栏项目搜索框过滤会话。
    """
    needle = (query or "").casefold().strip()
    if not needle:
        return True
    for raw in texts:
        hay = (raw or "").casefold()
        if not hay:
            continue
        if needle in hay:
            return True
        it = iter(hay)
        if all(ch in it for ch in needle):
            return True
    return False


def session_project_label(session: dict) -> str:
    """会话所属项目的展示名（cwd 末级目录；未知目录用统一文案）。"""
    cwd_key = normalize_cwd(session.get("cwd"))
    if not cwd_key:
        return str(session.get("cwd_display") or UNKNOWN_PROJECT_LABEL)
    base = os.path.basename(cwd_key)
    return base or str(session.get("cwd_display") or UNKNOWN_PROJECT_LABEL)


DEFAULT_SCAN_DEPTH = 4

# walk 时按目录名 SkipDir。`.stversions` / `.stfolder` 是 Syncthing 快照坑，必须硬编码。
HARD_SKIP_DIR_NAMES = frozenset({
    ".stversions",
    ".stfolder",
    ".git",
    ".cache",
    ".Trash",
    ".npm",
    ".nvm",
    ".local",
    ".cargo",
    ".rustup",
    ".pyenv",
    ".docker",
    ".venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".idea",
    ".vscode",
    "node_modules",
    "Library",
    "venv",
    "__pycache__",
    "dist",
    "build",
})

_PERSONAL_HOME_DIRS = ("Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music", "Library")
_DATA_PACKAGE_SUFFIXES = (".photoslibrary", ".photolibrary", ".musiclibrary")

_SOURCE_SESSION = "session"
_SOURCE_FILESYSTEM = "filesystem"

# 进程内缓存：同一配置下重复 discover 不重扫磁盘。
_fs_cache_key: tuple | None = None
_fs_cache_paths: list[str] | None = None


@dataclass(frozen=True)
class Project:
    """一条已发现项目。"""

    path: str
    name: str
    label: str
    sources: frozenset[str] = field(default_factory=frozenset)


class ProjectResolveError(Exception):
    """项目名匹配失败（0 命中、多命中无法交互、或多余参数）。"""


def configured_roots() -> list[str]:
    """默认 `$HOME`；`CORRAL_PROJECT_ROOTS` 覆盖（逗号分隔）。

    环境变量已设置但解析结果为空（例如 `CORRAL_PROJECT_ROOTS=`）时返回空列表，
    表示跳过文件系统扫描，只保留会话 cwd——便于测试与只要会话源的场景。
    """
    if not env_is_set("PROJECT_ROOTS"):
        return [os.path.expanduser("~")]
    raw = getenv("PROJECT_ROOTS") or ""
    return [os.path.expanduser(p.strip()) for p in raw.split(",") if p.strip()]


def configured_depth() -> int:
    raw = (getenv("PROJECT_DEPTH") or "").strip()
    if not raw:
        return DEFAULT_SCAN_DEPTH
    try:
        depth = int(raw)
    except ValueError:
        return DEFAULT_SCAN_DEPTH
    return depth if depth > 0 else DEFAULT_SCAN_DEPTH


def configured_extra_excludes() -> list[str]:
    raw = (getenv("PROJECT_EXCLUDE") or "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def clear_filesystem_cache() -> None:
    """测试用：清掉 git 扫描进程内缓存。"""
    global _fs_cache_key, _fs_cache_paths
    _fs_cache_key = None
    _fs_cache_paths = None


def _realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return os.path.normpath(path)


def _is_data_package(path: str) -> bool:
    return any(part.lower().endswith(_DATA_PACKAGE_SUFFIXES) for part in path.split(os.sep))


def _within_path(path: str, parent: str) -> bool:
    if sys.platform == "darwin":
        path, parent = path.casefold(), parent.casefold()
    return path == parent or path.startswith(parent + os.sep)


def _protected_scan_paths(root: str) -> tuple[str, ...]:
    # Construct exclusions without inspecting protected directories. A selected
    # root inside one is explicit scope; scanning its ancestor is not.
    home = _realpath(os.path.expanduser("~"))
    protected = [os.path.join(home, name) for name in _PERSONAL_HOME_DIRS]
    if sys.platform == "darwin":
        protected.append("/Volumes")
    return tuple(path for path in protected if not _within_path(root, path))


def _skip_scan_path(path: str, protected: tuple[str, ...]) -> bool:
    return _is_data_package(path) or any(_within_path(path, parent) for parent in protected)


def _resolve_scan_path(path: str, protected: tuple[str, ...]) -> str | None:
    # Like POSIX realpath, resolve components and relative symlink targets in
    # order. Unlike realpath, stop before any syscall inside a pruned subtree.
    absolute = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
    pending = absolute.split(os.sep)[::-1]
    resolved = os.sep
    links = 0
    while pending:
        part = pending.pop()
        if not part or part == ".":
            continue
        if part == "..":
            resolved = os.path.dirname(resolved)
            continue
        candidate = os.path.join(resolved, part)
        if _skip_scan_path(candidate, protected):
            return None
        try:
            target = os.readlink(candidate)
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                return None
            resolved = candidate
            continue
        links += 1
        if links > 40:
            return None
        if os.path.isabs(target):
            resolved = os.sep
        pending.extend(target.split(os.sep)[::-1])
    return resolved


def _should_skip_dir(name: str, abs_path: str, extra_excludes: list[str]) -> bool:
    if name in HARD_SKIP_DIR_NAMES:
        return True
    # 其它以 `.` 开头的目录一律不进（.stversions 已在硬排除里；这里兜住未见过的点目录）。
    if name.startswith("."):
        return True
    if not extra_excludes:
        return False
    slash = abs_path.replace("\\", "/")
    for pattern in extra_excludes:
        if pattern in {name, abs_path, slash}:
            return True
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(slash, pattern):
            return True
        # 路径任一段命中（如 exclude=vendor）
        if any(fnmatch.fnmatch(part, pattern) for part in slash.split("/") if part):
            return True
    return False


def _has_git_marker(path: str) -> bool:
    return os.path.lexists(os.path.join(path, ".git"))


def scan_git_roots(
    roots: Iterable[str] | None = None,
    *,
    depth: int | None = None,
    extra_excludes: Iterable[str] | None = None,
    allow_nested: bool = False,
    use_cache: bool = True,
) -> list[str]:
    """在配置根下递归找含 `.git` 的目录；命中后默认不进嵌套。返回排序后的绝对路径。"""
    global _fs_cache_key, _fs_cache_paths

    root_list = [os.path.expanduser(r) for r in (roots if roots is not None else configured_roots())]
    max_depth = configured_depth() if depth is None else depth
    excludes = list(extra_excludes) if extra_excludes is not None else configured_extra_excludes()
    cache_key = (tuple(root_list), max_depth, tuple(excludes), allow_nested)
    if use_cache and _fs_cache_key == cache_key and _fs_cache_paths is not None:
        return list(_fs_cache_paths)

    seen: dict[str, None] = {}
    for root in root_list:
        if not root or _is_data_package(root):
            continue
        root_clean = _resolve_scan_path(root, ())
        if root_clean is None:
            continue
        if not os.path.isdir(root_clean):
            continue
        _scan_one_root(root_clean, max_depth, excludes, allow_nested, seen)

    paths = sorted(seen.keys())
    if use_cache:
        _fs_cache_key = cache_key
        _fs_cache_paths = list(paths)
    return paths


def _scan_one_root(
    root: str,
    max_depth: int,
    extra_excludes: list[str],
    allow_nested: bool,
    seen: dict[str, None],
) -> None:
    """自实现 DFS 而非 `os.walk`：需要跟随目录软链接（如 `~/Codes -> /Users/…/Codes`），
    同时用 realpath 去重防成环，并保证收录的键是真实路径（与会话 cwd 对得上）。
    """
    # 根自身若是 git 项目也收录（深度 0）。
    if _has_git_marker(root):
        seen[normalize_cwd(root) or root] = None
        if not allow_nested:
            return

    visited: set[str] = {_realpath(root)}
    stack: list[tuple[str, int]] = [(root, 0)]
    protected = _protected_scan_paths(root)

    while stack:
        dirpath, depth = stack.pop()
        if depth >= max_depth:
            continue
        try:
            entries = list(os.scandir(dirpath))
        except OSError:
            continue
        for entry in entries:
            # Prune before is_dir(follow_symlinks=True) or .git probes: even a
            # metadata check through a symlink can trigger a privacy prompt.
            if _should_skip_dir(entry.name, entry.path, extra_excludes):
                continue
            if _skip_scan_path(entry.path, protected):
                continue
            try:
                linked = entry.is_symlink()
                if not linked and not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            child = _resolve_scan_path(entry.path, protected) if linked else entry.path
            if child is None:
                continue
            if _should_skip_dir(os.path.basename(child), child, extra_excludes):
                continue
            if linked and not os.path.isdir(child):
                continue
            if child in visited:
                continue
            visited.add(child)
            if _has_git_marker(child):
                seen[normalize_cwd(child) or child] = None
                if not allow_nested:
                    continue
            stack.append((child, depth + 1))


def discover(
    session_cwds: Iterable[str] | None = None,
    *,
    scan_filesystem: bool = True,
    roots: Iterable[str] | None = None,
    depth: int | None = None,
    extra_excludes: Iterable[str] | None = None,
    use_cache: bool = True,
) -> list[Project]:
    """合并会话 cwd 与 git 扫描结果，按 path 去重。"""
    by_path: dict[str, set[str]] = {}

    for raw in session_cwds or ():
        key = normalize_cwd(raw)
        if not key or is_ephemeral_agent_cwd(key):
            continue
        # 会话 cwd 即使目录已删也保留（与旧 _project_groups 一致）；启动时再校验。
        by_path.setdefault(key, set()).add(_SOURCE_SESSION)

    if scan_filesystem:
        for path in scan_git_roots(
            roots,
            depth=depth,
            extra_excludes=extra_excludes,
            use_cache=use_cache,
        ):
            key = normalize_cwd(path) or path
            by_path.setdefault(key, set()).add(_SOURCE_FILESYSTEM)

    named = [p for p in by_path if p]
    labels = disambiguate_labels(named)
    projects = [
        Project(
            path=path,
            name=os.path.basename(path) or path,
            label=labels.get(path, os.path.basename(path) or path),
            sources=frozenset(sources),
        )
        for path, sources in by_path.items()
    ]
    projects.sort(key=lambda p: (p.label.casefold(), p.path))
    return projects


# 子串命中（rank < _FUZZY_RANK_FLOOR）比"打散字符"的子序列命中强得多，
# 只要有子串命中就不再掺子序列结果——否则 `alpha` 会把 `java-platform` 这类
# 顺序恰好凑出 a-l-p-h-a 的项目也列进候选，把真正想要的淹掉。
_FUZZY_RANK_FLOOR = 4


def _match_rank(query: str, project: Project) -> int | None:
    """越小越优先；None 表示不匹配。"""
    needle = (query or "").casefold().strip()
    if not needle:
        return None
    name = project.name.casefold()
    label = project.label.casefold()
    path = project.path.casefold()
    if name == needle or label == needle:
        return 0
    if needle in name:
        return 1
    if needle in label:
        return 2
    if needle in path:
        return 3
    if fuzzy_match(query, project.name):
        return 4
    if fuzzy_match(query, project.label):
        return 5
    if fuzzy_match(query, project.path):
        return 6
    return None


def match_projects(query: str, projects: Iterable[Project]) -> list[Project]:
    """大小写无关模糊匹配；按相关度排序。"""
    ranked: list[tuple[int, Project]] = []
    for project in projects:
        rank = _match_rank(query, project)
        if rank is not None:
            ranked.append((rank, project))
    if any(rank < _FUZZY_RANK_FLOOR for rank, _ in ranked):
        ranked = [item for item in ranked if item[0] < _FUZZY_RANK_FLOOR]
    ranked.sort(key=lambda item: (item[0], item[1].label.casefold(), item[1].path))
    return [project for _, project in ranked]


def resolve_query(
    query: str,
    projects: Iterable[Project],
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    interactive: bool | None = None,
) -> str:
    """解析项目查询为唯一 cwd。0 命中 / 非交互多命中抛 ProjectResolveError。"""
    matches = match_projects(query, projects)
    if not matches:
        raise ProjectResolveError(t("project.not_found", query=query))
    if len(matches) == 1:
        return matches[0].path

    out = stdout or sys.stderr
    in_stream = stdin or sys.stdin
    if interactive is None:
        interactive = bool(getattr(in_stream, "isatty", lambda: False)())

    listing = "\n".join(
        f"  {index}. {project.label}  ({project.path})"
        for index, project in enumerate(matches, start=1)
    )
    if not interactive:
        raise ProjectResolveError(
            t("project.ambiguous", query=query, count=len(matches), listing=listing)
        )

    out.write(t("project.ambiguous_prompt", query=query, listing=listing))
    out.flush()

    while True:
        out.write(t("project.enter_number"))
        out.flush()
        line = in_stream.readline()
        if not line:
            raise ProjectResolveError(t("project.not_selected"))
        text = line.strip()
        if not text.isdigit():
            out.write(t("project.invalid_number"))
            continue
        choice = int(text)
        if 1 <= choice <= len(matches):
            return matches[choice - 1].path
        out.write(t("project.number_out_of_range", max=len(matches)))


def session_cwds_from_sessions(sessions_by_source: dict[str, list[dict]]) -> list[str]:
    """从扫描结果提取有效 cwd。"""
    out: list[str] = []
    seen: set[str] = set()
    for bucket in sessions_by_source.values():
        for session in bucket:
            key = normalize_cwd(session.get("cwd"))
            if not key or key in seen or is_ephemeral_agent_cwd(key):
                continue
            seen.add(key)
            out.append(key)
    return out


def project_entries(
    sessions_by_source: dict[str, list[dict]],
    *,
    scan_filesystem: bool = True,
    roots: Iterable[str] | None = None,
    depth: int | None = None,
    extra_excludes: Iterable[str] | None = None,
    use_cache: bool = True,
) -> list[dict]:
    """供 SessionStore / pick_project 使用的项目列表（兼容旧字段形状）。

    每项：cwd_key / label / count / latest_mtime；纯 git 项目 count=0。
    """
    stats: dict[str, dict] = {}
    for bucket in sessions_by_source.values():
        for session in bucket:
            key = normalize_cwd(session.get("cwd"))
            if not key or is_ephemeral_agent_cwd(key):
                # 未知目录仍留给旧逻辑：空 cwd 聚合在侧边栏意义不大，跳过。
                continue
            entry = stats.setdefault(key, {"count": 0, "latest_mtime": 0.0})
            entry["count"] += 1
            mtime = session.get("mtime") or 0
            if mtime > entry["latest_mtime"]:
                entry["latest_mtime"] = mtime

    # 无路径会话：保留「未知目录」桶（与旧 _project_groups 行为一致）。
    unknown_count = 0
    unknown_mtime = 0.0
    for bucket in sessions_by_source.values():
        for session in bucket:
            key = normalize_cwd(session.get("cwd"))
            if key:
                continue
            unknown_count += 1
            mtime = session.get("mtime") or 0
            if mtime > unknown_mtime:
                unknown_mtime = mtime

    discovered = discover(
        list(stats.keys()),
        scan_filesystem=scan_filesystem,
        roots=roots,
        depth=depth,
        extra_excludes=extra_excludes,
        use_cache=use_cache,
    )
    entries: list[dict] = []
    for project in discovered:
        st = stats.get(project.path, {"count": 0, "latest_mtime": 0.0})
        entries.append({
            "cwd_key": project.path,
            "label": project.label,
            "count": st["count"],
            "latest_mtime": st["latest_mtime"],
        })

    if unknown_count:
        entries.append({
            "cwd_key": "",
            "label": UNKNOWN_PROJECT_LABEL,
            "count": unknown_count,
            "latest_mtime": unknown_mtime,
        })

    return sorted(entries, key=lambda p: (-p["count"], -p["latest_mtime"], str(p["label"])))
