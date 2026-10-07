"""客户端自动更新：版本检查、安装渠道判定、就地升级与重启哨兵。

运行时无关、不依赖 ui 包，方便独立测试；网络与子进程调用一律吞掉异常返回
None/False，绝不能让检查或升级本身的故障拖死 TUI 或后台线程。只用标准库
（urllib/json/site/subprocess），不引入新依赖。
"""

from __future__ import annotations

import glob
import json
import os
import platform
import re
import shutil
import site
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Literal

from corral.legacy_names import cache_dir as product_cache_dir

REPO = "x0c/corral"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
TAG_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/tags/{{tag}}"
_FETCH_TIMEOUT = 3.0  # 秒；慢网络/无网不能拖住后台 worker
_ASSET_TIMEOUT = 8.0  # 秒；查 Release 资产列表比查最新版稍宽松，但仍不能挂住升级

CACHE_DIR = str(product_cache_dir())
STATE_FILE = os.path.join(CACHE_DIR, "update.json")

Channel = Literal["brew", "pipx", "pip", "dev"]


@dataclass(frozen=True)
class RestartRequest:
    """用户确认更新：先让 run_app() 退出，再由 cli.main() 完成升级并重启。

    包管理器会替换甚至删除当前进程正在使用的安装目录；若在 Textual worker 中
    原地升级，旧界面下一帧再惰性导入模块时就会随机崩溃。因此请求只携带升级所需
    的稳定值，真正的文件替换必须发生在界面主循环完全退出之后。
    """

    latest: str
    channel: Channel


def _version_tuple(text: str) -> tuple[int, ...]:
    """把 "0.20.0" / "v0.20.0" 这类字符串解析成可比较的整数元组。

    解析不出的片段按 0 处理，不因为一次奇怪的 tag 名（如带 -rc1 后缀）而抛异常。
    """
    text = text.strip()
    if text.startswith(("v", "V")):
        text = text[1:]
    parts = re.split(r"[.\-+]", text)
    out: list[int] = []
    for part in parts:
        match = re.match(r"\d+", part)
        if match:
            out.append(int(match.group()))
        else:
            break
    return tuple(out) or (0,)


def current_version() -> tuple[int, ...]:
    import corral

    return _version_tuple(corral.__version__)


def is_newer(latest: str, current: tuple[int, ...] | None = None) -> bool:
    """比较 latest（如 "0.20.1" 或 "v0.20.1"）是否严格新于当前版本。"""
    current = current if current is not None else current_version()
    return _version_tuple(latest) > current


def fetch_latest(timeout: float = _FETCH_TIMEOUT) -> str | None:
    """查询 GitHub 最新 release 的 tag_name，去掉前导 v；失败一律返回 None。"""
    try:
        req = urllib.request.Request(
            LATEST_RELEASE_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "corral-updater"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        tag = str(payload.get("tag_name") or "").strip()
        if not tag:
            return None
        return tag[1:] if tag[:1] in ("v", "V") else tag
    except (urllib.error.URLError, TimeoutError, ValueError, OSError, json.JSONDecodeError):
        return None


_BREW_MARKERS = ("/Cellar/", "/homebrew/", "linuxbrew")
_PIPX_PATH_MARKER = os.path.join("pipx", "venvs")


def _is_pipx_install(pkg_dir: str) -> bool:
    """当前 corral 是否装在 pipx 管理的隔离环境里。

    判据一是解释器所在 venv 根下的 `pipx_metadata.json`（与 PIPX_HOME 具体位置
    无关：macOS 在 ~/Library/Application Support/pipx，Linux 在 ~/.local/share/pipx）；
    判据二是包路径里出现 `pipx/venvs`，兜住从别处解释器 import 到该包的情形。

    必须先于 pip 判定：pipx venv 里的包路径同样含 site-packages，但那个 venv
    **默认不装 pip**，走 `python -m pip` 升级必然报 "No module named pip"。
    """
    if os.path.isfile(os.path.join(os.path.abspath(sys.prefix), "pipx_metadata.json")):
        return True
    return _PIPX_PATH_MARKER in pkg_dir


def detect_channel() -> Channel:
    """按 corral 包自身安装路径判定发布渠道：brew（Homebrew tap）/ pipx（隔离环境）/
    pip（用户或系统 site-packages）/ dev（源码检出或 editable 安装，无法一键升级）。"""
    import corral

    pkg_dir = os.path.dirname(os.path.abspath(corral.__file__))
    if any(marker in pkg_dir for marker in _BREW_MARKERS):
        return "brew"
    if _is_pipx_install(pkg_dir):
        return "pipx"
    site_dirs = [os.path.abspath(p) for p in _site_packages_dirs()]
    if any(pkg_dir.startswith(d) for d in site_dirs) or "site-packages" in pkg_dir:
        return "pip"
    return "dev"


def package_file() -> str:
    import corral

    return os.path.abspath(corral.__file__)


def find_checkout_root(start: str | None = None) -> str | None:
    """自 start（默认 cwd）向上找含 pyproject.toml + src/corral 的源码根。"""
    cur = os.path.abspath(start or os.getcwd())
    for _ in range(10):
        pyproject = os.path.join(cur, "pyproject.toml")
        init = os.path.join(cur, "src", "corral", "__init__.py")
        if os.path.isfile(pyproject) and os.path.isfile(init):
            try:
                with open(pyproject, encoding="utf-8") as fh:
                    text = fh.read(400)
                if 'name = "corral"' in text or "name = 'corral'" in text:
                    return cur
            except OSError:
                pass
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def is_loaded_from_checkout(checkout_root: str | None = None) -> bool:
    """当前 import 的 corral 是否来自给定（或 cwd 探测到的）源码树。"""
    root = checkout_root if checkout_root is not None else find_checkout_root()
    if not root:
        return False
    pkg = package_file()
    src = os.path.join(os.path.abspath(root), "src", "corral")
    try:
        return os.path.commonpath([pkg, src]) == src
    except ValueError:
        return False


def stale_source_warning(cwd: str | None = None) -> str | None:
    """在 corral 源码树内开发，但实际加载的是别处的安装副本时返回告警；否则 None。

    普通 pipx/brew 用户不在仓库里跑，不会触发。
    """
    root = find_checkout_root(cwd)
    if root is None:
        return None
    if is_loaded_from_checkout(root):
        return None
    script = os.path.join(root, "scripts", "dev-install.sh")
    return (
        f"正在仓库 {root} 中开发，但当前进程加载的是 {package_file()}，"
        f"改源码不会生效。请运行: bash {script} ，然后重启 TUI。"
    )


def install_report(cwd: str | None = None) -> dict:
    """供 diagnose / --version：版本、路径、渠道、是否与 cwd 源码树一致。"""
    import corral

    root = find_checkout_root(cwd)
    pkg = package_file()
    channel = detect_channel()
    from_checkout = is_loaded_from_checkout(root) if root else False
    editable_like = channel == "dev" or from_checkout
    warning = stale_source_warning(cwd)
    hints: list[str] = []
    if warning:
        hints.append(warning)
    elif root and not from_checkout:
        hints.append(
            f"开发安装请运行: bash {os.path.join(root, 'scripts', 'dev-install.sh')}"
        )
    elif not editable_like and channel in ("pip", "pipx"):
        hints.append(
            "若在改仓库源码，请用 scripts/dev-install.sh 做 editable 安装，"
            "不要只 force-reinstall 非 -e 副本"
        )
    return {
        "version": corral.__version__,
        "package_file": pkg,
        "python": sys.executable,
        "channel": channel,
        "checkout_root": root,
        "loaded_from_checkout": from_checkout,
        "editable_like": editable_like,
        "stale_source_warning": warning,
        "hints": hints,
    }


def _site_packages_dirs() -> list[str]:
    dirs: list[str] = []
    try:
        dirs.append(site.getusersitepackages())
    except Exception:
        pass
    try:
        dirs.extend(site.getsitepackages())
    except Exception:
        pass
    return [d for d in dirs if d]


def _is_user_site(pkg_dir: str) -> bool:
    try:
        user_site = os.path.abspath(site.getusersitepackages())
    except Exception:
        return False
    return pkg_dir.startswith(user_site)


def is_updatable(channel: Channel | None = None) -> bool:
    channel = channel or detect_channel()
    return channel in ("brew", "pipx", "pip")


# ---- 安装源：优先 Release 里的预编译包，源码兜底 ----
# corral 带 Rust 扩展，从源码装要求本机有完整 Rust 工具链，绝大多数用户没有。
# Release 已按平台附了 cp310-abi3 预编译包（与 install.sh 同一套匹配规则），
# 自动更新必须走同一条路，否则一键更新在干净机器上必然编译失败。

# ---- SessKit 预装：SessKit 尚未上 PyPI，任何 pip 解析路径都必须先给 pip
# 一个直链来源，否则 `sesskit>=…` 在 PyPI 上永远找不到（2026-09-30 v0.24.237
# Linux 更新实踩）。唯一权威是 scripts/sesskit_dep.py；updater 随包发到用户
# 机器、手边没有 scripts/，只能在这里镜像 pin。升级 SessKit 时两处同步改，
# 并同步 install.sh 的 curl|bash 硬编码 fallback。
_SESSKIT_VERSION = "0.2.9"
_SESSKIT_WHEEL_NAME = f"sesskit-{_SESSKIT_VERSION}-py3-none-any.whl"
_SESSKIT_WHEEL_SHA256 = "55152a0ceffc4fc5906acab56e5951ea0e57b1b5e3154e2f7cef9fdf4760b746"
_SESSKIT_WHEEL_URL = (
    f"https://github.com/x0c/sesskit/releases/download/v{_SESSKIT_VERSION}/{_SESSKIT_WHEEL_NAME}"
)


def sesskit_wheel_requirement() -> str:
    """pip 可直接安装的 SessKit 直链（含 digest，与 sesskit_dep.py 同源）。"""
    return f"sesskit @ {_SESSKIT_WHEEL_URL}#sha256={_SESSKIT_WHEEL_SHA256}"


def _ensure_sesskit_wheel() -> str | None:
    """把 pin 住的 SessKit wheel 取到本地缓存并校验 digest，返回文件路径。

    给更新子进程当 `PIP_FIND_LINKS` 用：pipx 1.0.0 会把 `--pip-args` 的值按
    空格拆散，`sesskit @ <url>` 这类带空格的直链传进去会被切碎（2026-09-30
    实踩 `@` 被当成独立 requirement），而本地路径无空格、两种拆分都安全。
    任何失败（无网、校验不对）一律返回 None，由调用方降级为旧行为，绝不抛异常。
    """
    import hashlib

    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        dest = os.path.join(CACHE_DIR, _SESSKIT_WHEEL_NAME)
        if os.path.isfile(dest):
            with open(dest, "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest() == _SESSKIT_WHEEL_SHA256:
                    return dest
        req = urllib.request.Request(
            _SESSKIT_WHEEL_URL, headers={"User-Agent": "corral-updater"}
        )
        with urllib.request.urlopen(req, timeout=_ASSET_TIMEOUT) as resp:
            data = resp.read()
        if hashlib.sha256(data).hexdigest() != _SESSKIT_WHEEL_SHA256:
            return None
        tmp_path = dest + f".tmp.{os.getpid()}"
        try:
            with open(tmp_path, "wb") as fh:
                fh.write(data)
            os.replace(tmp_path, dest)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return None
        return dest
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None

def _wheel_patterns(version: str) -> list[re.Pattern]:
    """按当前系统 / 架构给出候选包名正则，越靠前越优先。"""
    ver = re.escape(version)
    system = platform.system()
    machine = platform.machine().lower()
    arch = {
        "x86_64": "x86_64", "amd64": "x86_64",
        "arm64": "aarch64", "aarch64": "aarch64",
    }.get(machine)
    if system == "Darwin":
        # macOS 侧发的是 universal2，一份同时覆盖 Intel 与 Apple Silicon
        return [re.compile(rf"^corral-{ver}-cp310-abi3-macosx_.*universal2\.whl$")]
    if system == "Linux" and arch:
        musl = platform.libc_ver()[0] != "glibc" and bool(glob.glob("/lib/ld-musl-*.so.1"))
        family = "musllinux_1_2" if musl else "manylinux_2_17"
        # auditwheel 会同时附加新旧兼容标签，如 manylinux_2_17_x86_64.manylinux2014_x86_64
        return [re.compile(rf"^corral-{ver}-cp310-abi3-{family}_{arch}(\.\w+)?\.whl$")]
    return []


def release_asset_url(latest_tag: str, timeout: float = _ASSET_TIMEOUT) -> str | None:
    """查该 tag 的 Release 里适配本机的预编译包下载地址；查不到一律返回 None。"""
    patterns = _wheel_patterns(latest_tag.lstrip("vV"))
    if not patterns:
        return None
    try:
        req = urllib.request.Request(
            TAG_RELEASE_URL.format(tag=f"v{latest_tag.lstrip('vV')}"),
            headers={"Accept": "application/vnd.github+json", "User-Agent": "corral-updater"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assets = payload.get("assets") or []
    except (urllib.error.URLError, TimeoutError, ValueError, OSError, json.JSONDecodeError):
        return None
    for pattern in patterns:
        for asset in assets:
            name = str(asset.get("name") or "")
            url = asset.get("browser_download_url")
            if url and pattern.match(name):
                return str(url)
    return None


def install_spec(latest_tag: str) -> str:
    """pip / pipx 要安装的目标：优先预编译包，没有匹配时退回源码（需 Rust 工具链）。"""
    return release_asset_url(latest_tag) or f"git+https://github.com/{REPO}.git@v{latest_tag}"


def update_command(
    latest_tag: str, channel: Channel | None = None, *, spec: str | None = None
) -> list[str] | None:
    """返回执行就地升级的命令；dev 渠道无法自动升级，返回 None。"""
    channel = channel or detect_channel()
    if channel == "brew":
        return ["brew", "upgrade", "x0c/tap/corral"]
    if channel in ("pipx", "pip"):
        target = spec or install_spec(latest_tag)
        if channel == "pipx":
            # pipx 隔离环境里没有 pip，只能让 pipx 自己覆盖安装；--force 是覆盖
            # 已有同名应用的必需参数，pipx 会从包名推断应用名并重建 venv。
            # SessKit 预装不走 --pip-args（pipx 1.0.0 会按空格拆散它的值，带空格
            # 的直链会被切碎），而由 run_update 经 PIP_FIND_LINKS 给内部 pip 指路。
            return [shutil.which("pipx") or "pipx", "install", "--force", target]
        import corral

        pkg_dir = os.path.dirname(os.path.abspath(corral.__file__))
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]
        if _is_user_site(pkg_dir):
            cmd.append("--user")
        # SessKit 不在 PyPI：同一条 pip 命令里把直链一起带上，pip 用直链满足
        # `sesskit>=…`，不再去 PyPI 找（2026-09-30 v0.24.237 更新实踩）。
        cmd += [sesskit_wheel_requirement(), target]
        return cmd
    return None


def _brew_installed_version() -> tuple[int, ...] | None:
    """查询 Homebrew 中 corral 实际已安装的最高版本；查不到返回 None（此时不阻断，
    避免把「查询失败」误判成「升级失败」）。"""
    try:
        result = subprocess.run(
            ["brew", "list", "--versions", "x0c/tap/corral"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    # 输出形如 "corral 0.20.0 0.21.0"：首列是包名，其余是保留在本机的各版本
    versions = [_version_tuple(tok) for tok in (result.stdout or "").split()[1:]]
    versions = [v for v in versions if v != (0,)]
    return max(versions) if versions else None


def run_update(latest_tag: str, channel: Channel | None = None) -> tuple[bool, str]:
    """跑升级命令；返回 (是否成功, 合并输出摘要)。dev 渠道直接失败。

    pipx 渠道的坑：pipx venv 里没有 pip，任何 `python -m pip` 路线都会以
    "No module named pip" 失败，必须交给 pipx 自己覆盖安装。

    brew 渠道的两个坑都在这里兜住：
    1. 用户 shell 里常设 `HOMEBREW_NO_AUTO_UPDATE=1` 提速；子进程继承后
       `brew upgrade` 不刷新 tap，永远看不到刚发布的新配方 → 空跑退出 0（假成功）。
       这里为 brew 子进程强制去掉该变量，放开自动刷新。
    2. 即便如此，`brew upgrade` 什么都没升级也退出 0；必须核实实际安装版本确已
       到位，否则会误报「更新完成」，用户重启后仍是旧版本。
    """
    channel = channel or detect_channel()
    if channel == "pipx" and shutil.which("pipx") is None:
        return False, (
            "找不到 pipx 命令：corral 装在 pipx 隔离环境里，只能由 pipx 自己升级。"
            "请确认 pipx 在 PATH 中后重试，或手动执行：pipx upgrade corral"
        )
    cmd = update_command(latest_tag, channel)
    if cmd is None:
        return False, "当前安装方式（源码/开发安装）不支持一键更新"
    env = os.environ.copy()
    if channel == "brew":
        env.pop("HOMEBREW_NO_AUTO_UPDATE", None)
    if channel in ("pipx", "pip") and "PIP_FIND_LINKS" not in env:
        # SessKit 不在 PyPI：给 pip 系更新指一条本地 find-links，让内部 pip
        # 用缓存 wheel 满足 `sesskit>=…`（pipx 通道不能用 --pip-args 传直链，
        # 见 update_command 注释；2026-09-30 本机已用该办法从 0.24.232 升到
        # 0.24.238）。取不到缓存就降级为旧行为，错误自然冒出。用户显式设过的
        # PIP_FIND_LINKS 优先，不覆盖。
        wheel = _ensure_sesskit_wheel()
        if wheel:
            env["PIP_FIND_LINKS"] = os.path.dirname(wheel)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, env=env,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()[-4000:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, output
    if channel == "brew":
        installed = _brew_installed_version()
        if installed is not None and installed < _version_tuple(latest_tag):
            hint = "brew 未升级到最新版本，本地配方可能过期。请手动执行：brew update && brew upgrade x0c/tap/corral"
            return False, (output + "\n" + hint).strip()
    return True, output


# ---- 更新检查状态（当天忽略过就不再弹，次日恢复） ----

def _load_state() -> dict:
    if not os.path.isfile(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    """原子写：与 titles.py 的 save_cache 同一惯用法，避免并发读到半截 JSON。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp_path = STATE_FILE + f".tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATE_FILE)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _today() -> str:
    return date.today().isoformat()


def should_prompt(latest: str) -> bool:
    """latest 严格新于当前版本、且不满足"今天已经忽略过这个版本"时才应该弹窗。"""
    if not is_newer(latest):
        return False
    state = _load_state()
    return not (state.get("dismissed_version") == latest and state.get("dismissed_date") == _today())


def mark_dismissed(version: str) -> None:
    _save_state({"dismissed_version": version, "dismissed_date": _today()})


# ---- `corral update` 终端子命令 ----

def cli_update() -> int:
    """人读输出的终端手动升级入口，返回进程退出码。"""
    from corral.i18n import t

    channel = detect_channel()
    if not is_updatable(channel):
        print(t("update.cli_dev_hint", repo=REPO))
        return 1

    latest = fetch_latest()
    if latest is None:
        print(t("update.cli_check_failed"))
        return 1

    current = current_version()
    if not is_newer(latest, current):
        print(t("update.cli_latest", version=".".join(map(str, current))))
        return 0

    print(t("update.cli_updating", version=latest))
    ok, output = run_update(latest, channel)
    if output:
        print(output)
    if not ok:
        print(t("update.cli_failed"))
        return 1
    print(t("update.cli_updated", version=latest))
    return 0
