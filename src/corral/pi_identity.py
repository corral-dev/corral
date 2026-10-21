"""Pi 会话身份桥：Corral 自带扩展的幂等安装与 claim 读取。

背景：旧的每会话 ``--session-dir`` 隔离方案已废弃（破坏 Pi 原生 ``/resume``、
subagent 抢占主 pane）。替代方案是把 Pi 会话放回默认 cwd 目录，由随 Corral
打包、自动安装的全局 Pi 扩展 ``corral-session-identity`` 主动上报
「当前 TUI 进程 ↔ 当前会话」的 claim，Corral 只按 claim 精确绑定分屏。

完整设计见 ``docs/design/PI_SESSION_IDENTITY_EXTENSION_DESIGN.md``。

本模块只负责三件事，不读会话正文、不联网、不触碰 Corral namespace 之外的
任何文件：

1. ``ensure_extension_installed``：把包内 TypeScript 扩展资产原子安装/升级到
   Pi 全局扩展目录（幂等，Corral-owned 才可覆盖）；
2. claim 写入侧的配套参数（instance id、claim 路径、tmux 环境注入对）；
3. claim 读取与时效校验，供扫描器做精确 live 绑定；并读取可选
   ``agentPhase`` 字段，供关注圆点在 Working 尚未落盘时跟上 TUI。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from corral.runtime.base import LaunchError

PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
IDENTITY_DIRNAME = "corral-session-identity"
EXTENSION_ENTRY = "index.ts"
INSTANCE_ENV = "CORRAL_PI_INSTANCE_ID"
CLAIM_PATH_ENV = "CORRAL_PI_CLAIM_PATH"
PI_SESSION_DIR_ENV = "PI_CODING_AGENT_SESSION_DIR"
CLAIM_PROTOCOL = 1
EXTENSION_VERSION = "1.2.0"
MANIFEST_NAME = "corral-manifest.json"
#: 扩展 15s 心跳；超过 4 个周期未更新视为过期（含系统睡眠的短暂窗口）。
CLAIM_TTL_SECONDS = 60.0
_ASSET_NAME = "corral-session-identity.ts"


class PiExtensionInstallError(LaunchError):
    """扩展安装/校验失败：必须中止这条托管 Pi 启动，禁止静默降级为猜测绑定。"""


def pi_agent_dir(root: str | os.PathLike[str] | None = None) -> Path:
    """Pi 配置根：显式 root > ``PI_CODING_AGENT_DIR`` > ``~/.pi/agent``。"""
    if root is not None:
        return Path(root).expanduser()
    override = os.environ.get(PI_AGENT_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".pi" / "agent"


def extension_dir(root: str | os.PathLike[str] | None = None) -> Path:
    return pi_agent_dir(root) / "extensions" / IDENTITY_DIRNAME


def claims_dir(root: str | os.PathLike[str] | None = None) -> Path:
    return pi_agent_dir(root) / IDENTITY_DIRNAME / "claims" / "v1"


def claim_path_for(instance_id: str, root: str | os.PathLike[str] | None = None) -> Path:
    return claims_dir(root) / f"{instance_id}.json"


def asset_path() -> Path:
    """包内 TypeScript 扩展资产（wheel/pipx 安装后仍随包分发）。"""
    from corral import pi_extension

    return Path(pi_extension.__file__).resolve().parent / _ASSET_NAME


def new_instance_id() -> str:
    """pane 生命周期内稳定的 instance 标识，与 session id 解耦。"""
    return uuid.uuid4().hex


def instance_env_pairs(instance_id: str, root: str | os.PathLike[str] | None = None) -> list[str]:
    """``tmux new-session -e`` 参数：注入 instance id 与 claim 路径。"""
    return [
        "-e", f"{INSTANCE_ENV}={instance_id}",
        "-e", f"{CLAIM_PATH_ENV}={claim_path_for(instance_id, root)}",
        # 从旧版 Corral / 嵌套 Pi / 当前 shell 继承到的 session-dir 必须显式
        # 清空；只是不再新增还不够，否则新 Pi 仍会写回旧小房间。
        "-e", f"{PI_SESSION_DIR_ENV}=",
    ]


_MISSING_ASSET_MESSAGE = (
    "Corral 缺少 Pi 会话身份扩展资产，无法启用精确关联；"
    "请重新安装 Corral。/ Corral is missing its bundled Pi session "
    "identity extension; please reinstall Corral."
)


def _asset_bytes() -> bytes | None:
    """Packaged asset bytes; None when a replaced install deleted the package files.

    ImportError covers a host that never imported ``pi_extension`` before its
    install directory was removed.
    """
    try:
        return asset_path().read_bytes()
    except (OSError, ImportError):
        return None


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _is_installer_temp(name: str) -> bool:
    return name.startswith((f"{EXTENSION_ENTRY}.tmp-", f"{MANIFEST_NAME}.tmp-"))


def _adopt_interrupted_install(target_dir: Path, content_sha: str | None) -> bool:
    """A manifest-less directory is Corral's interrupted install when it holds only
    installer temp files plus, optionally, an entry byte-identical to the packaged
    asset. Stale temp files are removed so the caller can commit the manifest."""
    try:
        names = [child.name for child in target_dir.iterdir()]
    except OSError:
        return False
    if any(name != EXTENSION_ENTRY and not _is_installer_temp(name) for name in names):
        return False
    if EXTENSION_ENTRY in names and (
        content_sha is None or _file_sha256(target_dir / EXTENSION_ENTRY) != content_sha
    ):
        return False
    for name in names:
        if _is_installer_temp(name):
            try:
                (target_dir / name).unlink(missing_ok=True)
            except OSError:
                return False
    return True


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise PiExtensionInstallError(
            f"无法写入 Pi 会话身份扩展目录 {path.parent}：{exc}。请检查该目录的"
            f"写权限后重试。/ Cannot write the Pi session identity extension "
            f"directory {path.parent}: {exc}. Check directory permissions and retry."
        ) from exc


def _manifest_payload(content_sha256: str) -> dict:
    return {
        "owner": "corral",
        "extensionVersion": EXTENSION_VERSION,
        "claimProtocol": CLAIM_PROTOCOL,
        "sha256": content_sha256,
    }


def ensure_extension_installed(root: str | os.PathLike[str] | None = None) -> dict:
    """幂等安装/升级 Corral 自带的 Pi 身份扩展；失败抛 ``PiExtensionInstallError``。

    只有带 Corral owner manifest 的目录才允许覆盖升级；同名目录不属于
    Corral 时报冲突，绝不改写用户文件。manifest 最后提交：中断留下的
    半安装（只有与资产同字节的入口和安装临时文件）会在下次调用时被收敛。
    包内资产读不到时，已安装且协议一致的 Corral 扩展直接沿用。
    """
    target_dir = extension_dir(root)
    entry = target_dir / EXTENSION_ENTRY
    manifest_path = target_dir / MANIFEST_NAME
    asset = _asset_bytes()
    content_sha = hashlib.sha256(asset).hexdigest() if asset is not None else None

    manifest = _read_json(manifest_path)
    if manifest is None and target_dir.exists() and not _adopt_interrupted_install(target_dir, content_sha):
        # 非空目录存在但 manifest 缺失/损坏，且不是 Corral 中断留下的半安装：
        # 无法证明属于 Corral，禁止覆盖。
        raise PiExtensionInstallError(
            f"目录 {target_dir} 已存在但不属于 Corral（缺少或损坏的清单文件），"
            f"不会覆盖。请手动处理该目录后重试。/ Directory {target_dir} exists "
            f"but is not Corral-owned (missing or broken manifest); refusing to "
            f"overwrite. Resolve it manually and retry."
        )
    if manifest is not None and manifest.get("owner") != "corral":
        raise PiExtensionInstallError(
            f"目录 {target_dir} 的清单不属于 Corral（owner="
            f"{manifest.get('owner')!r}），不会覆盖。/ The manifest in "
            f"{target_dir} is not owned by Corral (owner={manifest.get('owner')!r}); "
            f"refusing to overwrite."
        )

    installed_sha = manifest.get("sha256") if manifest is not None else None
    installed_ok = bool(installed_sha) and _file_sha256(entry) == installed_sha
    if installed_ok and installed_sha == content_sha:
        return {"status": "ok", "path": str(entry), "version": EXTENSION_VERSION}
    if asset is None:
        # 资产只在安装/升级时需要：升级后仍在跑的旧进程读不到已被删除的包文件，
        # 但已装好的同协议扩展照样可用，不能因此拦下启动。
        if installed_ok and manifest.get("claimProtocol") == CLAIM_PROTOCOL:
            return {"status": "ok", "path": str(entry), "version": str(manifest.get("extensionVersion") or "")}
        raise PiExtensionInstallError(_MISSING_ASSET_MESSAGE)

    _atomic_write(entry, asset)
    _atomic_write(manifest_path, json.dumps(_manifest_payload(content_sha), indent=2).encode("utf-8"))
    # 回读验证：入口 + manifest 都在且内容匹配才算安装成功。
    if _read_json(manifest_path) is None or not entry.exists():
        raise PiExtensionInstallError(
            f"Pi 会话身份扩展安装后校验失败（{entry}）。请重试或检查磁盘状态。"
            f"/ Post-install verification of the Pi session identity extension "
            f"failed ({entry}). Retry or check the disk."
        )
    return {"status": "installed", "path": str(entry), "version": EXTENSION_VERSION}


def read_claims(root: str | os.PathLike[str] | None = None) -> list[dict]:
    """读取全部 claim 文件；损坏/半写的文件直接跳过（原子写保证极少出现）。"""
    directory = claims_dir(root)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    claims: list[dict] = []
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        claim = _read_json(directory / name)
        if claim is not None:
            claims.append(claim)
    return claims


def read_claim(instance_id: str, root: str | os.PathLike[str] | None = None) -> dict | None:
    """读取指定 instance 的 claim；instance id 做路径安全校验。"""
    text = str(instance_id or "").strip()
    if not text or "/" in text or ".." in text or text.startswith("."):
        return None
    return _read_json(claim_path_for(text, root))


def claim_is_live(claim: dict | None, now: datetime | None = None) -> bool:
    """claim 是否提供有效归属：协议匹配、状态 active/switching、心跳未过期。"""
    if not isinstance(claim, dict):
        return False
    if claim.get("protocolVersion") != CLAIM_PROTOCOL:
        return False
    if claim.get("state") not in ("active", "switching"):
        return False
    session_id = str(claim.get("sessionId") or "").strip()
    if not session_id:
        return False
    try:
        pid = int(claim.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    updated = _parse_iso(claim.get("updatedAt"))
    if updated is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    return now - updated <= timedelta(seconds=CLAIM_TTL_SECONDS)


def live_agent_phases(
    root: str | os.PathLike[str] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, dict[str, str]]:
    """Map live claim sessionId → agent phase fields for attention dots.

    Optional claim fields ``agentPhase`` / ``agentPhaseEvent`` / ``agentPhaseAt``
    (extension 1.1+) report TUI Working before jsonl catches up. Missing or
    invalid phase is omitted so history-only inspectors keep working.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    out: dict[str, dict[str, str]] = {}
    for claim in read_claims(root):
        if not claim_is_live(claim, now):
            continue
        session_id = str(claim.get("sessionId") or "").strip()
        phase = str(claim.get("agentPhase") or "").strip()
        if not session_id or phase not in {"idle", "working", "waiting"}:
            continue
        out[session_id] = {
            "phase": phase,
            "event": str(claim.get("agentPhaseEvent") or ""),
            "at": str(claim.get("agentPhaseAt") or claim.get("updatedAt") or ""),
        }
    return out


def _parse_iso(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
