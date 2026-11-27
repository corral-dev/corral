"""局域网地址与端口：配对二维码、hello 与状态输出共用的唯一真源。

只回答两件事：这台机器在局域网上的真实地址有哪些、直连服务实际用哪个端口。
不碰监听 socket、不做 mDNS 广播（见 `mdns.py`），无第三方依赖。

为什么独立成模块：旧实现（`transport/local.py`）把 UDP 探针结果不加过滤就写进
配对二维码；在有 VPN/透明代理的机器上探针会返回脏地址（如 OpenClash fake-ip
段 198.18.0.0/15），手机拿到连不上的地址只能回退中继。这里用标准库
`ipaddress` 做分类过滤，真局域网优先，其它单播地址保留在后。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import socket
import sys
import threading
from ipaddress import ip_address, ip_network
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from corral.remote.config import RemoteState

#: LocalServer 未显式指定端口时的监听端口（`remote on --port` 可覆盖）。
DEFAULT_LOCAL_PORT = 8737

#: 配对二维码 / hello 里最多带几条局域网提示。
MAX_LOCAL_HINTS = 3

#: Hostname resolution is a supplement, never a gate: `getaddrinfo` has no
#: timeout of its own and blocks for seconds when the name does not resolve
#: (5.00 s measured on macOS for an unregistered `.local` name — the norm on a
#: CI runner). ``hello`` and the pairing payload call :func:`lan_addresses` on
#: a request path, so the probe answer stands on its own after this bound.
_HOSTNAME_LOOKUP_SECONDS = 0.2
_hostname_lock = threading.Lock()
_hostname_in_flight = False

_RFC1918 = (
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
)
#: 透明代理 fake-ip 常用段（RFC 2544 基准测试段，常被本机代理占用）：绝不发给手机。
_FAKE_IP = ip_network("198.18.0.0/15")

_IFF_UP = 0x1
_IFF_LOOPBACK = 0x8
_IFF_RUNNING = 0x40
_SKIP_INTERFACE_PREFIXES = (
    "lo",
    "awdl",
    "llw",
    "anpi",
    "ap",
    "bridge",
    "docker",
    "br-",
    "veth",
    "virbr",
    "podman",
    "cni",
    "flannel",
)


class _IfAddrs(ctypes.Structure):
    pass


_IfAddrs._fields_ = [
    ("ifa_next", ctypes.POINTER(_IfAddrs)),
    ("ifa_name", ctypes.c_char_p),
    ("ifa_flags", ctypes.c_uint),
    ("ifa_addr", ctypes.c_void_p),
    ("ifa_netmask", ctypes.c_void_p),
    ("ifa_ifu", ctypes.c_void_p),
    ("ifa_data", ctypes.c_void_p),
]

_libc_lock = threading.Lock()
_libc_loaded = False
_libc_handle: ctypes.CDLL | None = None


def _get_libc() -> ctypes.CDLL | None:
    global _libc_loaded, _libc_handle
    with _libc_lock:
        if _libc_loaded:
            return _libc_handle
        _libc_loaded = True
        try:
            if sys.platform == "darwin":
                lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            elif sys.platform.startswith("linux"):
                lib = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
            else:
                return None
            lib.getifaddrs.argtypes = [ctypes.POINTER(ctypes.POINTER(_IfAddrs))]
            lib.getifaddrs.restype = ctypes.c_int
            lib.freeifaddrs.argtypes = [ctypes.POINTER(_IfAddrs)]
            lib.freeifaddrs.restype = None
            _libc_handle = lib
        except (OSError, AttributeError):
            _libc_handle = None
        return _libc_handle


def _should_skip_interface(name: str) -> bool:
    lower = name.lower()
    return any(lower.startswith(prefix) for prefix in _SKIP_INTERFACE_PREFIXES)


def _interface_addresses() -> list[str]:
    """Enumerate active non-loopback IPv4 interface addresses via POSIX getifaddrs.

    Includes private overlay interfaces (such as macOS ZeroTier ``feth*`` or
    Tailscale ``utun*``) that neither the default-route probe nor hostname
    resolution returns.
    """
    lib = _get_libc()
    if lib is None:
        return []
    head = ctypes.POINTER(_IfAddrs)()
    if lib.getifaddrs(ctypes.byref(head)) != 0:
        return []
    found: list[str] = []
    try:
        current = head
        while current:
            entry = current.contents
            flags = int(entry.ifa_flags)
            name = entry.ifa_name.decode("utf-8", "ignore") if entry.ifa_name else ""
            if (
                (flags & _IFF_UP)
                and (flags & _IFF_RUNNING)
                and not (flags & _IFF_LOOPBACK)
                and name
                and not _should_skip_interface(name)
                and entry.ifa_addr
            ):
                raw = ctypes.string_at(entry.ifa_addr, 8)
                family = raw[1] if sys.platform == "darwin" else int.from_bytes(raw[0:2], sys.byteorder)
                if family == socket.AF_INET:
                    found.append(socket.inet_ntoa(raw[4:8]))
            current = entry.ifa_next
    finally:
        lib.freeifaddrs(head)
    return found


def effective_local_port(state: RemoteState) -> int:
    """直连实际端口：`remote.json` 只在 `on --port` 时写，0 表示走默认。"""
    try:
        return int(getattr(state, "local_port", 0) or DEFAULT_LOCAL_PORT)
    except (TypeError, ValueError):
        return DEFAULT_LOCAL_PORT


def _rank(address: object) -> tuple[int, str] | None:
    """地址分类：家用 Wi-Fi 段优先，再其它 RFC1918，再叠加组网/公网；脏地址丢掉。

    同属 RFC1918 时把 ``192.168/16``、``172.16/12`` 排在 ``10/8`` 前面——后者常被
    ZeroTier / 虚拟网卡占用；手机同 Wi-Fi 抢答应先试真正的局域网地址。
    """
    try:
        ip = ip_address(str(address).strip())
    except ValueError:
        return None
    if ip.version != 4:
        return None
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return None
    if ip in _FAKE_IP:
        return None
    if ip in ip_network("192.168.0.0/16"):
        return (0, str(ip))
    if ip in ip_network("172.16.0.0/12"):
        return (1, str(ip))
    if ip in ip_network("10.0.0.0/8"):
        return (2, str(ip))
    # Tailscale / 其它私有组网与公网地址落在这里：能用，排后面。
    return (3, str(ip))


def probe_outbound_ipv4() -> str:
    """Return the OS-selected outbound IPv4 address without sending packets or DNS."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1 文档保留地址，不会真的发包
        return str(probe.getsockname()[0])
    except OSError:
        return ""
    finally:
        probe.close()


def lan_addresses() -> list[str]:
    """列出这台开发机在局域网上的地址，真局域网在前，去重并排好序。

    先用一次到外网地址的 UDP「连接」（不产生任何流量）问操作系统默认出口，
    再用 hostname 解析和 POSIX `getifaddrs` 活动网卡枚举补充（让 ZeroTier `feth*`、
    Tailscale 等私有组网地址也能被手机拿到）；全部候选经 `_rank` 过滤排序，
    VPN/代理的脏地址进不了结果。
    """
    candidates: list[str] = []
    primary = probe_outbound_ipv4()
    if primary:
        candidates.append(primary)
    candidates.extend(_hostname_addresses())
    candidates.extend(_interface_addresses())
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for raw in candidates:
        item = _rank(raw)
        if item is None or item[1] in seen:
            continue
        seen.add(item[1])
        ranked.append(item)
    ranked.sort()
    return [text for _, text in ranked]


def _hostname_addresses() -> list[str]:
    """What the hostname resolves to, bounded; an empty answer is allowed.

    One lookup runs at a time: a caller arriving while the bounded wait is
    still running gets the probe-only answer instead of stacking resolver
    threads on a machine whose name never resolves.
    """
    global _hostname_in_flight
    with _hostname_lock:
        if _hostname_in_flight:
            return []
        _hostname_in_flight = True
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    if not hostname:
        with _hostname_lock:
            _hostname_in_flight = False
        return []
    found: list[str] = []
    done = threading.Event()

    def _resolve() -> None:
        global _hostname_in_flight
        addresses: list[str] = []
        try:
            try:
                infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
            except Exception:  # noqa: BLE001 - a name we cannot resolve proves nothing
                infos = []
            for info in infos:
                try:
                    addresses.append(str(info[4][0]))
                except (IndexError, TypeError):
                    continue
        finally:
            found.extend(addresses)
            with _hostname_lock:
                _hostname_in_flight = False
            done.set()

    threading.Thread(target=_resolve, name="corral-lan-hints", daemon=True).start()
    done.wait(_HOSTNAME_LOOKUP_SECONDS)
    return list(found)


def local_hints(port: int) -> list[str]:
    """`"ip:port"` 提示列表（配对 `l=` / hello `local_hints` 共用），最多三条。"""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return []
    if port <= 0:
        return []
    return [f"{address}:{port}" for address in lan_addresses()[:MAX_LOCAL_HINTS]]
