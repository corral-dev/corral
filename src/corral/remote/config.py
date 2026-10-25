"""开发机侧的持久化状态：本机身份密钥、已配对设备、中继地址。

正式路径在状态目录（`CORRAL_STATE_DIR` > `XDG_STATE_HOME` > `~/.local/state/corral/remote`），
不再放缓存目录——缓存可被清理工具随时删掉，私钥与配对清单丢了等于全部手机失效。
测试与显式覆盖仍认 `CORRAL_CACHE_DIR/remote`，并自动从旧缓存路径迁一次。

**私钥与状态文件一律以 0600 直接创建**（`os.open`），避免「先按 umask 写出再 chmod」
的可读窗口。目录 0700。私钥只在本机落盘，任何情况下都不写日志、不进遥测、不随诊断输出。

设备清单的写入一律「读最新 → 叠加本次改动 → 写回」，禁止拿进程内陈旧快照整份覆盖——
否则 `corral remote unpair`（另一进程）会被常驻服务下次 `touch_device` 静默撤销。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from corral.i18n import t
from corral.legacy_names import env_is_set, getenv, state_dir
from corral.split_layout import layout_cache_dir

# Open-source default: no shared relay. Fresh installs are LAN-only until the
# operator sets their own wss:// URL. Never ship a maintainer hostname here.
_DEFAULT_RELAY_URL = ""
_STATE_FILENAME = "remote.json"
_KEY_FILENAME = "identity.key"
_HOST_KEY_FILENAME = "host.key"
_HOST_PREV_KEY_FILENAME = "host.key.prev"
_ACCOUNT_FILENAME = "account.json"
_PAIRING_FILENAME = "pairing.json"
_PID_FILENAME = "remote.pid"
_STATUS_FILENAME = "remote-status.json"

# 必须是 RLock：load_state 持锁时会再进 load_or_create_identity / host_key，
# 普通 Lock 在空状态目录（测试、首次启动）会死锁。
_lock = threading.RLock()
_state_mtime: int = -1


def _file_mtime_token(path: Path) -> int:
    """状态文件变更令牌：用纳秒 mtime，避免同秒多次写入被当成「没变」。"""
    return path.stat().st_mtime_ns



def remote_dir() -> Path:
    """状态目录：优先显式覆盖，否则用 XDG 状态目录，并兼容旧缓存路径。"""
    if env_is_set("STATE_DIR"):
        return Path(getenv("STATE_DIR") or "").expanduser() / "remote"
    if env_is_set("CACHE_DIR"):
        return Path(getenv("CACHE_DIR") or "").expanduser() / "remote"
    primary = state_dir() / "remote"
    legacy = layout_cache_dir() / "remote"
    _migrate_legacy_dir(legacy, primary)
    return primary


def _migrate_legacy_dir(legacy: Path, primary: Path) -> None:
    """把旧 `~/.cache/corral/remote` 一次性迁到状态目录；目标已存在则不动。"""
    if primary.exists() or not legacy.exists():
        return
    try:
        primary.parent.mkdir(parents=True, exist_ok=True)
        legacy.rename(primary)
    except OSError:
        try:
            primary.mkdir(parents=True, exist_ok=True)
            for name in (
                _STATE_FILENAME,
                _KEY_FILENAME,
                _HOST_KEY_FILENAME,
                _HOST_PREV_KEY_FILENAME,
                _PAIRING_FILENAME,
                _PID_FILENAME,
            ):
                src = legacy / name
                if src.is_file() and not (primary / name).exists():
                    _atomic_write_bytes(primary / name, src.read_bytes(), 0o600)
        except OSError:
            pass


def state_path() -> Path:
    return remote_dir() / _STATE_FILENAME


def identity_key_path() -> Path:
    return remote_dir() / _KEY_FILENAME


def host_key_path() -> Path:
    return remote_dir() / _HOST_KEY_FILENAME


def host_prev_key_path() -> Path:
    """轮换后尚未被中继确认的上一把注册钥匙。"""
    return remote_dir() / _HOST_PREV_KEY_FILENAME


def account_path() -> Path:
    return remote_dir() / _ACCOUNT_FILENAME


def pairing_path() -> Path:
    return remote_dir() / _PAIRING_FILENAME


def pid_path() -> Path:
    return remote_dir() / _PID_FILENAME


def status_snapshot_path() -> Path:
    return remote_dir() / _STATUS_FILENAME


def write_status_snapshot(payload: dict) -> None:
    """常驻服务把在线设备与最近操作写成快照，供 `corral remote status` 跨进程读取。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    _atomic_write_text(status_snapshot_path(), text, 0o600)


def read_status_snapshot() -> dict | None:
    try:
        raw = json.loads(status_snapshot_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def clear_status_snapshot() -> None:
    try:
        status_snapshot_path().unlink()
    except OSError:
        pass


def _ensure_dir() -> Path:
    directory = remote_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def _atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """以指定权限直接创建临时文件再替换，避免 umask 窗口。"""
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"), mode)


def sanitize_display_name(value: str, *, max_len: int = 60, fallback: str = "device") -> str:
    """去掉控制字符与 ANSI 转义，避免设备名污染终端输出 / 中继日志。"""
    text = str(value or "")
    cleaned = "".join(
        ch for ch in text if ch.isprintable() and ord(ch) >= 32 and ch not in "\x1b"
    ).strip()[:max_len]
    return cleaned or fallback


@dataclass
class PairedDevice:
    """一台已经配对过的手机。

    `public_key` 是设备的 X25519 公钥（十六进制）。`push_token` 只有在设备允许
    推送后才有值。`access` 为 `full`（默认可读写）或 `readonly`（只能看）。
    """

    id: str
    name: str
    public_key: str
    paired_at: float
    last_seen_at: float = 0.0
    push_token: str = ""
    push_env: str = ""
    platform: str = ""
    access: str = "full"
    # 完成/中断通知偏好：缺字段=开（老设备不受影响）。False 才不发。
    notify_completed: bool = True
    notify_aborted: bool = True
    # 可自动重试的临时异常：缺字段=关（机主 2026-10-25 决定），显式 True 才发。
    notify_transient: bool = False

    @classmethod
    def from_dict(cls, raw: dict) -> PairedDevice:
        access = str(raw.get("access") or "full").strip().lower()
        if access not in ("full", "readonly"):
            access = "full"
        return cls(
            id=str(raw.get("id") or ""),
            name=sanitize_display_name(str(raw.get("name") or ""), fallback="iPhone"),
            public_key=str(raw.get("public_key") or ""),
            paired_at=float(raw.get("paired_at") or 0.0),
            last_seen_at=float(raw.get("last_seen_at") or 0.0),
            push_token=str(raw.get("push_token") or ""),
            push_env=str(raw.get("push_env") or ""),
            platform=sanitize_display_name(str(raw.get("platform") or ""), max_len=20, fallback=""),
            access=access,
            notify_completed=False if raw.get("notify_completed") is False else True,
            notify_aborted=False if raw.get("notify_aborted") is False else True,
            notify_transient=raw.get("notify_transient") is True,
        )


@dataclass
class RemoteState:
    """`remote.json` 的完整内容。私钥单独存文件，不在这里。"""

    host_id: str = ""
    host_name: str = ""
    relay_url: str = _DEFAULT_RELAY_URL
    relay_enabled: bool = False
    local_enabled: bool = True
    local_port: int = 0
    # Remembered switch: True means login/reboot must bring the service back.
    wanted: bool = False
    cwd_whitelist: list[str] = field(default_factory=list)
    devices: list[PairedDevice] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["devices"] = [asdict(d) for d in self.devices]
        return data

    @classmethod
    def from_dict(cls, raw: dict) -> RemoteState:
        host_id = str(raw.get("host_id") or "")
        whitelist_raw = raw.get("cwd_whitelist") or []
        whitelist = [str(p) for p in whitelist_raw if isinstance(p, str) and str(p).strip()]
        return cls(
            host_id=host_id,
            host_name=sanitize_display_name(
                str(raw.get("host_name") or ""), max_len=80, fallback="dev"
            ),
            relay_url=str(raw.get("relay_url") or _DEFAULT_RELAY_URL),
            # Missing key → LAN-only open-source default. Existing installs keep
            # whatever they already persisted (including maintainer private relays).
            relay_enabled=bool(raw["relay_enabled"])
            if "relay_enabled" in raw
            else False,
            local_enabled=bool(raw.get("local_enabled", True)),
            local_port=int(raw.get("local_port") or 0),
            wanted=bool(raw.get("wanted", False)),
            cwd_whitelist=whitelist,
            devices=[
                PairedDevice.from_dict(d) for d in raw.get("devices") or [] if isinstance(d, dict)
            ],
        )


def default_host_name() -> str:
    for getter in (lambda: socket.gethostname(), lambda: os.uname().nodename):  # type: ignore[attr-defined]
        try:
            name = str(getter() or "").strip()
        except Exception:
            continue
        if name:
            if name.endswith(".local"):
                name = name[: -len(".local")]
            return sanitize_display_name(name, max_len=80, fallback="dev")
    return "dev"


def _read_state_unlocked() -> RemoteState:
    path = state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return RemoteState.from_dict(raw if isinstance(raw, dict) else {})


def _write_state_unlocked(state: RemoteState) -> None:
    global _state_mtime
    payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
    path = state_path()
    previous = _state_mtime
    _atomic_write_text(path, payload, 0o600)
    try:
        token = _file_mtime_token(path)
        # 远程盘 / virtiofs 等可能在同刻写入后仍返回旧 mtime_ns，unpair 会被漏掉。
        if token == previous:
            st = path.stat()
            os.utime(path, ns=(st.st_atime_ns, token + 1))
            token = _file_mtime_token(path)
        _state_mtime = token
    except OSError:
        _state_mtime = time.time_ns()


def load_state() -> RemoteState:
    with _lock:
        state = _read_state_unlocked()
        changed = False
        if not state.host_name:
            state.host_name = default_host_name()
            changed = True
        try:
            from corral.remote.crypto import available, public_key_bytes, routing_id_from_x25519

            if available():
                derived = routing_id_from_x25519(public_key_bytes(load_or_create_identity()))
                if state.host_id != derived:
                    state.host_id = derived
                    changed = True
                load_or_create_host_key()
        except Exception:
            pass
        if changed:
            _write_state_unlocked(state)
        else:
            try:
                global _state_mtime
                _state_mtime = _file_mtime_token(state_path())
            except OSError:
                pass
        return state


def state_mtime() -> int:
    """当前状态文件的变更令牌；不存在时返回 -1。供常驻服务自持「上次加载」对照。"""
    try:
        return _file_mtime_token(state_path())
    except OSError:
        return -1


def read_state_from_disk() -> RemoteState:
    """无条件从磁盘读一份，不碰模块级「上次加载」缓存。"""
    with _lock:
        return _read_state_unlocked()


def reload_state_if_changed(
    current: RemoteState | None = None, *, known_mtime: int | None = None
) -> RemoteState:
    """磁盘状态文件变了就重读。

    写入方也会更新模块级 `_state_mtime`，同进程里「CLI 改盘、服务持旧快照」时
    不能靠那个全局值判断。调用方应传入自己上次加载时的 `known_mtime`。
    令牌取自纳秒 mtime：秒级分辨率下同秒多次写入会漏掉 unpair。
    """
    global _state_mtime
    path = state_path()
    try:
        mtime = _file_mtime_token(path)
    except OSError:
        return load_state()
    baseline = known_mtime if known_mtime is not None else _state_mtime
    if current is not None and mtime == baseline:
        return current
    with _lock:
        state = _read_state_unlocked()
        _state_mtime = mtime
        return state


def save_state(state: RemoteState) -> None:
    with _lock:
        _write_state_unlocked(state)


def load_or_create_identity() -> bytes:
    path = identity_key_path()
    try:
        data = path.read_bytes()
        if len(data) == 32:
            return data
    except OSError:
        pass
    from corral.remote.crypto import generate_private_key_bytes

    key = generate_private_key_bytes()
    with _lock:
        _atomic_write_bytes(path, key, 0o600)
    return key


def _read_key_file(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
        if len(data) == 32:
            return data
    except OSError:
        pass
    return None


def load_or_create_host_key() -> bytes:
    path = host_key_path()
    existing = _read_key_file(path)
    if existing is not None:
        return existing
    from corral.remote.crypto import generate_host_key_bytes

    key = generate_host_key_bytes()
    with _lock:
        _atomic_write_bytes(path, key, 0o600)
    return key


def add_device(state: RemoteState, device: PairedDevice) -> RemoteState:
    device.name = sanitize_display_name(device.name, fallback="iPhone")
    device.platform = sanitize_display_name(device.platform, max_len=20, fallback="")
    if device.access not in ("full", "readonly"):
        device.access = "full"
    with _lock:
        fresh = _read_state_unlocked()
        devices = [d for d in fresh.devices if d.public_key != device.public_key]
        devices.append(device)
        fresh.devices = devices
        _write_state_unlocked(fresh)
        state.devices = list(fresh.devices)
        return state


def remove_device(state: RemoteState, device_id: str) -> bool:
    with _lock:
        fresh = _read_state_unlocked()
        before = len(fresh.devices)
        fresh.devices = [d for d in fresh.devices if d.id != device_id]
        if len(fresh.devices) == before:
            return False
        _write_state_unlocked(fresh)
        state.devices = list(fresh.devices)
        return True


def touch_device(state: RemoteState, public_key: str, **updates: object) -> PairedDevice | None:
    """更新设备字段。必须读磁盘最新清单再叠加，避免 unpair 被陈旧快照撤销。"""
    with _lock:
        fresh = _read_state_unlocked()
        for device in fresh.devices:
            if device.public_key != public_key:
                continue
            device.last_seen_at = time.time()
            for key, value in updates.items():
                if value not in (None, "") and hasattr(device, key):
                    if key in ("name", "platform"):
                        value = sanitize_display_name(
                            str(value),
                            max_len=20 if key == "platform" else 60,
                            fallback=getattr(device, key) or key,
                        )
                    setattr(device, key, value)
            _write_state_unlocked(fresh)
            state.devices = list(fresh.devices)
            return device
        return None


def find_device(state: RemoteState, public_key: str) -> PairedDevice | None:
    for device in state.devices:
        if device.public_key == public_key:
            return device
    return None


def find_device_by_id(state: RemoteState, device_id: str) -> PairedDevice | None:
    for device in state.devices:
        if device.id == device_id:
            return device
    return None


def load_host_prev_key() -> bytes | None:
    """轮换后尚未被中继确认的上一把注册钥匙。"""
    return _read_key_file(host_prev_key_path())


def clear_host_prev_key() -> None:
    with _lock:
        try:
            host_prev_key_path().unlink()
        except OSError:
            pass


def rotate_host_key(state: RemoteState) -> RemoteState:
    """轮换 Ed25519 注册密钥。routing_id 由 X25519 派生，已配对手机不必重扫。

    若旧钥匙仍可能登记在中继上，先写入 ``host.key.prev``，下次握手成功后再删。
    连续轮换且尚未连上中继时，保留中继上那把旧钥匙，不把中间版本当 prev。
    """
    from corral.remote.crypto import generate_host_key_bytes

    new_key = generate_host_key_bytes()
    with _lock:
        old_key = _read_key_file(host_key_path())
        prev = host_prev_key_path()
        if old_key is not None and not prev.exists():
            _atomic_write_bytes(prev, old_key, 0o600)
        _atomic_write_bytes(host_key_path(), new_key, 0o600)
    return state


def is_public_relay(url: str) -> bool:
    """Whether this relay requires multi-tenant account login.

    Open-source default allowlist is empty so no maintainer hostname is baked
    into the repo. Operators who run their own multi-tenant relay can set
    ``CORRAL_PUBLIC_RELAY_URLS`` to a comma-separated list of wss/https bases.
    """
    cleaned = str(url or "").strip().rstrip("/").lower()
    if not cleaned:
        return False
    allow = str(getenv("PUBLIC_RELAY_URLS") or "").strip()
    if not allow:
        return False
    allowed = {
        item.strip().rstrip("/").lower()
        for item in allow.split(",")
        if item.strip()
    }
    return cleaned in allowed


def load_account() -> dict:
    try:
        raw = json.loads(account_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_account(payload: dict) -> None:
    _atomic_write_text(account_path(), json.dumps(payload, ensure_ascii=False, indent=2), 0o600)


def clear_account() -> None:
    try:
        account_path().unlink()
    except OSError:
        pass


def validate_relay_url(url: str, *, allow_insecure: bool = False) -> str:
    cleaned = str(url or "").strip().rstrip("/")
    if not cleaned:
        raise ValueError(t("remote.err.relay_url_empty"))
    if cleaned.startswith("wss://"):
        return cleaned
    if cleaned.startswith("ws://"):
        if allow_insecure:
            return cleaned
        raise ValueError(t("remote.err.relay_url_insecure"))
    raise ValueError(t("remote.err.relay_url_scheme"))


def write_pairing(code: str, ttl: float, *, mode: str = "full") -> None:
    if mode not in ("full", "readonly"):
        mode = "full"
    payload = json.dumps({"code": code, "expires_at": time.time() + ttl, "mode": mode})
    with _lock:
        _atomic_write_text(pairing_path(), payload, 0o600)


def _pairing_mode_of(raw: dict) -> str:
    mode = str(raw.get("mode") or "full").strip().lower()
    return mode if mode in ("full", "readonly") else "full"


def _read_pairing_unlocked() -> dict | None:
    try:
        raw = json.loads(pairing_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        _clear_pairing_unlocked()
        return None
    code = str(raw.get("code") or "")
    expires_at = float(raw.get("expires_at") or 0.0)
    if not code or expires_at <= time.time():
        _clear_pairing_unlocked()
        return None
    return raw


def read_pairing_mode() -> str:
    with _lock:
        window = _read_pairing_unlocked()
        if window is None:
            return "full"
        return _pairing_mode_of(window)


def read_pairing() -> tuple[str, float] | None:
    with _lock:
        window = _read_pairing_unlocked()
        if window is None:
            return None
        return str(window.get("code") or ""), float(window.get("expires_at") or 0.0)


def consume_pairing(submitted: str) -> tuple[str | None, str | None]:
    """同一把锁里读码、比对、删窗口。返回 (mode, error)，error 为 expired / wrong / None。"""
    from corral.remote.crypto import codes_equal

    with _lock:
        window = _read_pairing_unlocked()
        if window is None:
            return None, "expired"
        stored = str(window.get("code") or "")
        if not codes_equal(submitted, stored):
            return None, "wrong"
        _clear_pairing_unlocked()
        return _pairing_mode_of(window), None


def clear_pairing() -> None:
    with _lock:
        _clear_pairing_unlocked()


def _clear_pairing_unlocked() -> None:
    try:
        pairing_path().unlink()
    except OSError:
        pass


def write_pid() -> None:
    _atomic_write_text(pid_path(), str(os.getpid()), 0o600)


def read_pid() -> int | None:
    try:
        pid = int(pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def clear_pid() -> None:
    try:
        pid_path().unlink()
    except OSError:
        pass
