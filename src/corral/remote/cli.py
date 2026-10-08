"""`corral remote` 子命令：把开发机接到手机上。

远程服务是开关语义：`on` 打开（后台常驻，命令立刻返回）、`off` 关掉，
都幂等。配对二维码只走 `pair`，不跟开关联动。

退出码沿用 corral 既有的一套：0 成功、1 一般失败、2 用法错误、3 资源不存在
（`unpair` 找不到设备）。带 `--json` 的子命令输出与 `agent_api` 同形状的
envelope（`{ok, data, error{code,message,hint,next_commands}, meta{version}}`），
方便脚本和管家 Agent 调用。登录/中继失败仍是 exit 1，用 `error.code`
（`account_error`/`missing_dependencies` 等）区分，不靠解析文案。

副作用命令（`on`/`off`/`pair`/`unpair`/`rotate-key`/`rename`）支持 `--dry-run`：
只演练不更改，输出形状与真实执行一致（多一个 `dry_run: true`，`changed` 恒为 false）。
`login`/`logout` 是鉴权引导步骤，不支持 `--dry-run`。

兼容别名：`start`→`on`、`stop`→`off`（行为已改为开关，不再前台占终端、
也不再顺带刷配对码）。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from contextlib import suppress

from corral import updater
from corral.i18n import join_names, t
from corral.remote import autostart as remote_autostart
from corral.remote import config as remote_config
from corral.remote import crypto, pairing

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3

REMOTE_API_VERSION = 1

_PAIRING_TTL = 10 * 60
_READY_WAIT_SECONDS = 15.0
_READY_POLL_SECONDS = 0.05


def _bin() -> str:
    """当前 corral 入口的绝对路径：next_commands 保持它，而不是裸命令名。

    与 agent_api._bin 同逻辑（独立实现，避免 remote 提前拖入整棵 runtime 依赖）：
    pipx 与源码树两份 corral 并存是常态，裸 `corral remote …` 可能命中旧副本。
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0.endswith("__main__.py"):
        return f"{sys.executable} -m corral"
    if argv0:
        candidate = argv0 if os.path.isabs(argv0) else os.path.abspath(argv0)
        try:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            pass
    import shutil

    return shutil.which("corral") or "corral"


def _is_dry_run(args) -> bool:
    """读 --dry-run 开关。必须用 `is True`：测试里 args 常是裸 Mock，未传的属性是 truthy Mock。"""
    return getattr(args, "dry_run", False) is True


def _envelope(ok: bool, data=None, *, code: str = "ok", message: str = "",
              hint: str | None = None, next_commands: list[str] | None = None) -> str:
    """与 agent_api 同形状的 envelope：{ok, data, error{code,message,hint,next_commands}, meta}。

    只加字段不改旧语义：成功沿用 {ok,data}，失败的 error 在旧 {message} 上追加
    code/hint/next_commands，老调用方只读 message 照样工作。
    """
    if ok:
        error = None
    else:
        error = {
            "code": code,
            "message": message,
            "hint": hint,
            "next_commands": list(next_commands or []),
        }
    return json.dumps(
        {"ok": ok, "data": data if ok else None, "error": error,
         "meta": {"version": REMOTE_API_VERSION}},
        ensure_ascii=False,
        indent=2,
    )


def _fail(message: str, as_json: bool = False, *, code: str = "remote_error",
          hint: str | None = None, next_commands: list[str] | None = None,
          exit_code: int = EXIT_ERROR) -> int:
    if as_json:
        print(_envelope(False, code=code, message=message, hint=hint,
                        next_commands=next_commands))
    else:
        print(message, file=sys.stderr)
        if hint:
            print(hint, file=sys.stderr)
    return exit_code


def _dependency_hint(missing: list[str]) -> tuple[str, list[str]]:
    """缺失组件的 hint + 可直接执行的手动安装命令（next_commands 照抄即可）。"""
    cmd = shlex.join(_dependency_install_command(missing))
    hint = t("remote.deps.install_hint", packages=" ".join(missing), command=cmd)
    return hint, [cmd]


def _access_label(access: str) -> str:
    if access == "readonly":
        return t("remote.access.readonly")
    return t("remote.access.full")


def _missing_dependencies() -> list[str]:
    """返回当前运行解释器中尚未安装的远程能力组件。"""
    missing = []
    if not crypto.available():
        missing.append("cryptography")
    try:
        import websockets  # noqa: F401
    except ImportError:
        missing.append("websockets")
    try:
        import segno  # noqa: F401
    except ImportError:
        missing.append("segno")
    return missing


def _check_dependencies() -> str:
    """只读检查缺失组件，供状态类命令使用。"""
    missing = _missing_dependencies()
    if not missing:
        return ""
    return t(
        "remote.deps.missing",
        names=join_names(missing),
        packages=" ".join(missing),
    )


def _dependency_install_command(missing: list[str]) -> list[str]:
    """按实际安装渠道生成能改到同一运行环境的安装命令。"""
    if updater.detect_channel() == "pipx":
        # pipx 的 venv 默认没有 pip；必须由 pipx 注入到当前 corral 环境。
        return ["pipx", "inject", "corral", *missing]
    return [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        *missing,
    ]


def _ensure_dependencies() -> str:
    """在当前 Corral 运行环境中幂等补齐启动远程服务所需组件。"""
    missing = _missing_dependencies()
    if not missing:
        return ""
    print(t("remote.deps.installing", names=join_names(missing)), file=sys.stderr)
    try:
        result = subprocess.run(
            _dependency_install_command(missing),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return t("remote.deps.auto_install_failed")
    if result.returncode != 0 or _missing_dependencies():
        return t("remote.deps.auto_install_failed")
    print(t("remote.deps.installed", names=join_names(missing)), file=sys.stderr)
    return ""


def _stop_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(20):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except OSError:
            return
    with suppress(OSError):
        os.kill(pid, signal.SIGKILL)


def _validate_service_flags(args, state: remote_config.RemoteState) -> str | None:
    """把 on 上的中继/局域网开关写进 state（只改内存）；失败返回错误文案。"""
    if args.relay_url:
        try:
            state.relay_url = remote_config.validate_relay_url(
                args.relay_url, allow_insecure=args.insecure_relay
            )
        except ValueError as exc:
            return str(exc)
    elif state.relay_enabled and not args.no_relay:
        try:
            state.relay_url = remote_config.validate_relay_url(
                state.relay_url, allow_insecure=args.insecure_relay
            )
        except ValueError as exc:
            return str(exc)
    if args.no_relay:
        state.relay_enabled = False
    if args.no_local:
        state.local_enabled = False
    if args.port:
        state.local_port = int(args.port)
    return None


def _apply_service_flags(args, state: remote_config.RemoteState) -> str | None:
    """校验开关并落盘；dry-run 请用 _validate_service_flags 作用在副本上，不要调这里。"""
    error = _validate_service_flags(args, state)
    if error is not None:
        return error
    remote_config.save_state(state)
    return None


def _print_on_status(state: remote_config.RemoteState, pid: int | None) -> None:
    print(t("remote.on.ready", name=state.host_name, pid=pid or "?"))
    if state.relay_enabled:
        print(t("remote.on.relay", url=state.relay_url))
    if state.local_enabled:
        print(t("remote.on.local_on"))
    if not state.devices:
        print(t("remote.on.pair_hint"))


def _run_daemon_foreground(state: remote_config.RemoteState) -> int:
    from corral import observe
    from corral.remote.daemon import RemoteDaemon

    observe.install_stack_dumps()  # 活进程诊断：kill -USR1 <pid> 取全线程栈到 stacks.log。

    daemon = RemoteDaemon(state)
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def _serve_argv() -> list[str]:
    """子进程 argv：同一解释器里的 `python -m corral remote _serve`。"""
    return [sys.executable, "-m", "corral", "remote", "_serve"]


def _spawn_background_daemon() -> subprocess.Popen:
    """脱离当前终端拉起常驻服务；与标题后台进程同一套路。"""
    return subprocess.Popen(
        _serve_argv(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )


def _wait_until_running(timeout: float = _READY_WAIT_SECONDS) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = remote_config.read_pid()
        if pid:
            return pid
        time.sleep(_READY_POLL_SECONDS)
    return remote_config.read_pid()


# ---------------------------------------------------------------------------
# on / off / _serve
# ---------------------------------------------------------------------------

def _remember_wanted(state: remote_config.RemoteState, wanted: bool) -> None:
    state.wanted = wanted
    remote_config.save_state(state)


def _register_with_relay(state: remote_config.RemoteState, skip_relay: bool = False) -> str | None:
    """Register this host with its relay when one is on; returns the error message."""
    if not state.relay_enabled or skip_relay:
        return None
    from corral.remote import account as remote_account

    ok, message = remote_account.register_host(state)
    return None if ok else message


def _start_background(autostart_error: str | None) -> tuple[int | None, str]:
    """Wait for the autostart unit, else spawn the service; returns (pid, error)."""
    pid = _wait_until_running(timeout=2.0) if not autostart_error else None
    child = None
    if not pid:
        try:
            child = _spawn_background_daemon()
        except OSError as exc:
            return None, t("remote.on.spawn_failed", error=exc)
        pid = _wait_until_running()
    if not pid:
        # 子进程可能立刻挂了；尽量带上它的退出码。
        code = child.poll() if child is not None else None
        detail = f"exit {code}" if code is not None else (autostart_error or "no pid")
        return None, t("remote.on.not_ready", detail=detail)
    return pid, ""


def _ensure_service_on(state: remote_config.RemoteState) -> tuple[int | None, bool, str, str]:
    """Turn remote on for pairing, as `on` would; returns (pid, started, error, code)."""
    running = remote_config.read_pid()
    if running:
        _remember_wanted(state, True)
        remote_autostart.enable(restart_existing=False)
        return running, False, "", "ok"
    if state.relay_enabled:
        try:
            # A saved ws:// relay was already opted into with --insecure-relay.
            state.relay_url = remote_config.validate_relay_url(state.relay_url, allow_insecure=True)
        except ValueError as exc:
            return None, False, str(exc), "usage_error"
    _remember_wanted(state, True)
    registration_error = _register_with_relay(state)
    if registration_error:
        return None, False, registration_error, "account_error"
    pid, error = _start_background(remote_autostart.enable())
    return pid, bool(pid), error, "ok" if pid else "remote_error"


def _cmd_on(args) -> int:
    dry_run = _is_dry_run(args)
    if dry_run:
        # 演练分支必须全程只读：不自动安装依赖（只报缺什么）、不写开关记忆、
        # 不拉起守护进程、不注册主机。输出形状与真实执行一致 + dry_run 标记。
        missing = _missing_dependencies()
        if missing:
            hint, next_commands = _dependency_hint(missing)
            return _fail(_check_dependencies(), args.json, code="missing_dependencies",
                         hint=hint, next_commands=next_commands)
        state = remote_config.load_state()
        flag_error = _validate_service_flags(args, copy.copy(state))
        if flag_error:
            return _fail(flag_error, args.json, code="usage_error", exit_code=EXIT_USAGE)
        running = remote_config.read_pid()
        data = {
            "enabled": True,
            "wanted": True,
            "running": bool(running),
            "autostart": remote_autostart.is_installed(),
            "pid": running,
            "changed": False,
            "dry_run": True,
            "would": "already_running" if running else "start",
            "host_name": state.host_name,
        }
        if args.json:
            print(_envelope(True, data))
        elif not args.quiet:
            print(t("remote.dry_run.banner"))
            if running:
                print(t("remote.dry_run.on_running", pid=running))
            else:
                print(t("remote.dry_run.on_start"))
        return EXIT_OK

    problem = _ensure_dependencies()
    if problem:
        missing = _missing_dependencies()
        hint, next_commands = _dependency_hint(missing) if missing else (None, None)
        return _fail(problem, args.json, code="missing_dependencies",
                     hint=hint, next_commands=next_commands)

    state = remote_config.load_state()
    flag_error = _apply_service_flags(args, state)
    if flag_error:
        return _fail(flag_error, args.json, code="usage_error", exit_code=EXIT_USAGE)

    _remember_wanted(state, True)
    state = remote_config.load_state()

    running = remote_config.read_pid()
    if running and not args.force:
        remote_autostart.enable()
        data = {
            "enabled": True,
            "wanted": True,
            "running": True,
            "autostart": remote_autostart.is_installed(),
            "pid": running,
            "changed": False,
            "host_name": state.host_name,
        }
        if args.json:
            print(_envelope(True, data))
        elif not args.quiet:
            print(t("remote.on.already", pid=running))
            print(t("remote.on.remembered"))
        return EXIT_OK

    if running and args.force:
        remote_autostart.disable()
        _stop_pid(running)
        remote_config.clear_pid()

    registration_error = _register_with_relay(state, args.no_relay)
    if registration_error:
        return _fail(registration_error, args.json, code="account_error",
                     next_commands=[f"{_bin()} remote status --json"])

    # Arm OS autostart before starting so a crash mid-on still comes back after reboot.
    autostart_error = remote_autostart.enable()

    if args.foreground:
        if args.json:
            # 前台占进程时 JSON 呼叫方拿不到回包；先打一行再进主循环。
            print(
                _envelope(
                    True,
                    {
                        "enabled": True,
                        "wanted": True,
                        "running": True,
                        "autostart": remote_autostart.is_installed(),
                        "pid": os.getpid(),
                        "changed": True,
                        "foreground": True,
                        "host_name": state.host_name,
                    },
                )
            )
            sys.stdout.flush()
        elif not args.quiet:
            _print_on_status(state, os.getpid())
            print(t("remote.on.remembered"))
            print(t("remote.on.foreground_hint"))
        return _run_daemon_foreground(state)

    pid, start_error = _start_background(autostart_error)
    if not pid:
        return _fail(start_error, args.json)

    data = {
        "enabled": True,
        "wanted": True,
        "running": True,
        "autostart": remote_autostart.is_installed(),
        "pid": pid,
        "changed": True,
        "host_name": state.host_name,
    }
    if args.json:
        print(_envelope(True, data))
    elif not args.quiet:
        _print_on_status(state, pid)
        print(t("remote.on.remembered"))
    return EXIT_OK


def _cmd_off(args) -> int:
    state = remote_config.load_state()
    if _is_dry_run(args):
        # 演练：不写开关记忆、不解 armed、不杀进程。
        pid = remote_config.read_pid()
        data = {
            "enabled": False,
            "wanted": False,
            "running": bool(pid),
            "autostart": remote_autostart.is_installed(),
            "pid": pid,
            "changed": False,
            "dry_run": True,
            "would": "already_off" if not pid else "stop",
        }
        if args.json:
            print(_envelope(True, data))
        elif not getattr(args, "quiet", False):
            print(t("remote.dry_run.banner"))
            if pid:
                print(t("remote.dry_run.off_running", pid=pid))
            else:
                print(t("remote.off.already"))
        return EXIT_OK
    _remember_wanted(state, False)
    # Disarm first so KeepAlive / systemd Restart cannot race the stop.
    remote_autostart.disable()

    pid = remote_config.read_pid()
    if not pid:
        data = {
            "enabled": False,
            "wanted": False,
            "running": False,
            "autostart": remote_autostart.is_installed(),
            "pid": None,
            "changed": False,
        }
        if args.json:
            print(_envelope(True, data))
        elif not getattr(args, "quiet", False):
            print(t("remote.off.already"))
        return EXIT_OK
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return _fail(t("remote.off.failed", error=exc), args.json)
    # 等进程真正退出，避免立刻 on 时撞上旧 pid 文件。
    for _ in range(50):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    else:
        with suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    remote_config.clear_pid()
    data = {
        "enabled": False,
        "wanted": False,
        "running": False,
        "autostart": remote_autostart.is_installed(),
        "pid": pid,
        "changed": True,
    }
    if args.json:
        print(_envelope(True, data))
    else:
        print(t("remote.off.done", pid=pid))
    return EXIT_OK


def _cmd_serve(_args) -> int:
    """内部入口：后台子进程跑常驻服务；不进 --help。"""
    problem = _ensure_dependencies()
    if problem:
        print(problem, file=sys.stderr)
        return EXIT_ERROR
    state = remote_config.load_state()
    if not state.wanted:
        # Stale unit after off, or manual _serve — do not linger under KeepAlive.
        remote_autostart.disable()
        return EXIT_OK
    existing = remote_config.read_pid()
    if existing and existing != os.getpid():
        # Another live instance owns the switch; exit cleanly for launchd/systemd.
        return EXIT_OK
    return _run_daemon_foreground(state)


def _print_pairing(state, code: str, public_key: bytes, local_port: int, *, mode: str = "full") -> None:
    url = pairing.build_payload(state, code, public_key, local_port)
    qr = pairing.render_qr(url)
    mode_hint = t("remote.pair.readonly_hint") if mode == "readonly" else ""
    print(t("remote.pair.scan", mode_hint=mode_hint))
    if qr:
        print(qr)
        print(t("remote.pair.link", url=url))
        print(t("remote.pair.code_manual", code=code))
    else:
        print(pairing.render_fallback(url, code))
    print(t("remote.pair.valid_ten_minutes"))
    print(t("remote.pair.trust_warning"))


# ---------------------------------------------------------------------------
# pair
# ---------------------------------------------------------------------------

def _cmd_pair(args) -> int:
    mode = "readonly" if getattr(args, "readonly", False) else "full"
    if _is_dry_run(args):
        # 演练：不装依赖、不创建本机身份（load_or_create_identity 会写文件）、
        # 不 mint 配对码、不写配对窗口。返回预检式预览（服务状态 + 依赖），
        # 而不是形状相同但 code 为假的载荷——假码会被调用方当真去扫。
        missing = _check_dependencies()
        if missing:
            hint, next_commands = _dependency_hint(_missing_dependencies())
            return _fail(missing, args.json, code="missing_dependencies",
                         hint=hint, next_commands=next_commands)
        running = bool(remote_config.read_pid())
        data = {
            "service_running": running,
            "would_start_service": not running,
            "dependencies_ok": True,
            "mode": mode,
            "dry_run": True,
        }
        if args.json:
            print(_envelope(True, data))
        else:
            print(t("remote.dry_run.banner"))
            print(t("remote.dry_run.pair", mode=_access_label(mode)))
        return EXIT_OK
    problem = _ensure_dependencies()
    if problem:
        missing = _missing_dependencies()
        hint, next_commands = _dependency_hint(missing) if missing else (None, None)
        return _fail(problem, args.json, code="missing_dependencies",
                     hint=hint, next_commands=next_commands)
    public_key = crypto.public_key_bytes(remote_config.load_or_create_identity())
    # A code is useless until the service runs, so pairing turns remote on first.
    service_pid, started, error, error_code = _ensure_service_on(remote_config.load_state())
    if not service_pid:
        return _fail(error, args.json, code=error_code,
                     next_commands=[f"{_bin()} remote status --json"])
    state = remote_config.load_state()
    code = crypto.new_pairing_code()
    remote_config.write_pairing(code, _PAIRING_TTL, mode=mode)
    if args.json:
        payload = json.loads(pairing.as_json(state, code, public_key, state.local_port))
        payload["access"] = mode
        payload["service_started"] = started
        payload["service_pid"] = service_pid
        print(_envelope(True, payload))
        return EXIT_OK
    if started:
        print(t("remote.pair.service_started", pid=service_pid))
    _print_pairing(state, code, public_key, state.local_port, mode=mode)
    return EXIT_OK


# ---------------------------------------------------------------------------
# status / devices / unpair / rotate-key / account
# ---------------------------------------------------------------------------

def _cmd_status(args) -> int:
    from corral.remote.lan import effective_local_port, local_hints

    state = remote_config.load_state()
    pid = remote_config.read_pid()
    window = remote_config.read_pairing()
    effective_port = effective_local_port(state) if state.local_enabled else 0
    snapshot = remote_config.read_status_snapshot() if pid else None
    snapshot_hints: list[str] = []
    if isinstance(snapshot, dict) and isinstance(snapshot.get("local_hints"), list):
        snapshot_hints = [str(h) for h in snapshot["local_hints"] if str(h).strip()]
    if not snapshot_hints and state.local_enabled and effective_port:
        snapshot_hints = local_hints(effective_port)
    if isinstance(snapshot, dict) and snapshot.get("local_port"):
        try:
            effective_port = int(snapshot["local_port"])
        except (TypeError, ValueError):
            pass
    data = {
        "running": bool(pid),
        "enabled": bool(pid),
        "wanted": bool(state.wanted),
        "autostart": remote_autostart.is_installed(),
        "pid": pid,
        "host_id": state.host_id,
        "host_name": state.host_name,
        "relay_url": state.relay_url if state.relay_enabled else "",
        "relay_enabled": state.relay_enabled,
        "local_enabled": state.local_enabled,
        "local_port": effective_port,
        "local_hints": snapshot_hints,
        "devices": len(state.devices),
        "pairing_open": bool(window),
        "pairing_mode": remote_config.read_pairing_mode() if window else "",
        "dependencies_ok": not _check_dependencies(),
        "device_list": [
            {
                "id": d.id,
                "name": d.name,
                "access": d.access,
                "last_seen_at": d.last_seen_at,
            }
            for d in state.devices
        ],
    }
    account = remote_config.load_account()
    data["account"] = {
        "login": account.get("login") or "",
        "account_id": account.get("account_id") or "",
        "quota": account.get("quota") or {},
    }
    if snapshot:
        data["online"] = snapshot.get("online") or []
        data["recent"] = snapshot.get("recent") or []
        data["relay_online"] = bool(snapshot.get("relay_online"))
        data["relay_connected_at"] = snapshot.get("relay_connected_at")
        data["relay_error"] = snapshot.get("relay_error") or ""
    elif state.relay_enabled:
        data["relay_online"] = False
        data["relay_connected_at"] = None
        data["relay_error"] = ""
    if args.json:
        print(_envelope(True, data))
        return EXIT_OK
    print(t("remote.status.host", name=state.host_name))
    print(t("remote.status.routing", id=state.host_id))
    if account.get("login"):
        print(t("remote.status.account", login=account.get("login")))
    elif state.relay_enabled and remote_config.is_public_relay(state.relay_url):
        print(t("remote.status.account_none"))
    state_label = t("remote.status.running") if pid else t("remote.status.not_running")
    pid_suffix = t("remote.status.pid_suffix", pid=pid) if pid else ""
    print(t("remote.status.line", state=state_label, pid_suffix=pid_suffix))
    if state.wanted:
        print(
            t("remote.status.wanted_on")
            if remote_autostart.is_installed()
            else t("remote.status.wanted_on_no_autostart")
        )
    else:
        print(t("remote.status.wanted_off"))
    if not state.relay_enabled:
        print(t("remote.status.relay_off"))
    else:
        relay_label = state.relay_url
        if snapshot is not None:
            if snapshot.get("relay_online"):
                connected_at = snapshot.get("relay_connected_at")
                since = ""
                if isinstance(connected_at, (int, float)) and connected_at:
                    since = t(
                        "remote.status.relay_since",
                        time=time.strftime("%H:%M:%S", time.localtime(connected_at)),
                    )
                print(t("remote.status.relay_online", label=relay_label, since=since))
            else:
                err = snapshot.get("relay_error") or ""
                suffix = t("remote.status.relay_error_suffix", error=err) if err else ""
                print(t("remote.status.relay_offline", label=relay_label, suffix=suffix))
        else:
            print(t("remote.status.relay_unknown", label=relay_label))
    print(
        t("remote.status.local_on")
        if state.local_enabled
        else t("remote.status.local_off")
    )
    if state.local_enabled and snapshot_hints:
        print(t("remote.status.local_hint", hint=snapshot_hints[0]))
    if isinstance(snapshot, dict) and snapshot.get("mdns"):
        from corral.remote.mdns import SERVICE_TYPE, service_name

        print(
            t(
                "remote.status.mdns_on",
                service=service_name(state.host_id) + "." + SERVICE_TYPE,
            )
        )
    print(t("remote.status.paired_count", count=len(state.devices)))
    for device in state.devices:
        print(
            t(
                "remote.status.device_item",
                name=device.name or device.id,
                access=_access_label(device.access),
            )
        )
    if snapshot:
        online = snapshot.get("online") or []
        print(t("remote.status.online_count", count=len(online)))
        for entry in online:
            name = entry.get("name") or entry.get("id") or "?"
            addr = entry.get("address") or ""
            suffix = t("remote.status.online_addr", addr=addr) if addr else ""
            print(
                t(
                    "remote.status.online_item",
                    name=name,
                    access=_access_label(entry.get("access") or ""),
                    suffix=suffix,
                )
            )
        recent = snapshot.get("recent") or []
        if recent:
            print(t("remote.status.recent_header"))
            for entry in recent[-8:]:
                ts = entry.get("ts") or 0
                stamp = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"
                line = f"  {stamp}  {entry.get('device') or '?'}  {entry.get('method') or '?'}"
                duration = entry.get("duration_ms")
                if isinstance(duration, bool):
                    duration = None
                if isinstance(duration, (int, float)):
                    line += f"  {int(duration)}ms"
                plane = entry.get("plane")
                if plane in ("control", "data"):
                    line += f"  [{plane}]"
                print(line)
    if window:
        remaining = int(window[1] - time.time())
        mode = remote_config.read_pairing_mode()
        print(
            t(
                "remote.status.pairing_window",
                mode=_access_label(mode),
                seconds=max(0, remaining),
            )
        )
    if pid:
        print(t("remote.status.kick_hint"))
    problem = _check_dependencies()
    if problem:
        print("\n" + problem)
    return EXIT_OK


def _cmd_devices(args) -> int:
    state = remote_config.load_state()
    devices = [
        {
            "id": d.id,
            "name": d.name,
            "platform": d.platform,
            "access": d.access,
            "paired_at": d.paired_at,
            "last_seen_at": d.last_seen_at,
            "push": bool(d.push_token),
        }
        for d in state.devices
    ]
    if args.json:
        print(_envelope(True, {"devices": devices}))
        return EXIT_OK
    if not devices:
        print(t("remote.devices.empty"))
        return EXIT_OK
    for device in devices:
        last = (
            time.strftime("%m-%d %H:%M", time.localtime(device["last_seen_at"]))
            if device["last_seen_at"]
            else t("remote.devices.never")
        )
        push = t("remote.devices.push_on") if device["push"] else t("remote.devices.push_off")
        print(
            t(
                "remote.devices.line",
                id=device["id"],
                name=device["name"],
                access=_access_label(device["access"]),
                last=last,
                push=push,
            )
        )
    return EXIT_OK


def _cmd_unpair(args) -> int:
    state = remote_config.load_state()
    device = remote_config.find_device_by_id(state, args.device_id)
    if device is None:
        return _fail(t("remote.unpair.not_found", device_id=args.device_id), args.json,
                     code="not_found", exit_code=EXIT_NOT_FOUND,
                     hint=t("remote.unpair.hint"),
                     next_commands=[f"{_bin()} remote devices --json"])
    if _is_dry_run(args):
        data = {
            "device_id": args.device_id,
            "device_name": getattr(device, "name", "") or args.device_id,
            "removed": False,
            "dry_run": True,
        }
        if args.json:
            print(_envelope(True, data))
        else:
            print(t("remote.dry_run.banner"))
            print(t("remote.dry_run.unpair", name=data["device_name"]))
        return EXIT_OK
    if not remote_config.remove_device(state, args.device_id):
        return _fail(t("remote.unpair.not_found", device_id=args.device_id), args.json,
                     code="not_found", exit_code=EXIT_NOT_FOUND,
                     hint=t("remote.unpair.hint"),
                     next_commands=[f"{_bin()} remote devices --json"])
    message = t("remote.unpair.done")
    data = {"device_id": args.device_id, "removed": True}
    print(_envelope(True, data) if args.json else message)
    return EXIT_OK


def _cmd_rename(args) -> int:
    state = remote_config.load_state()
    dry_run = _is_dry_run(args)
    if getattr(args, "clear", False):
        new_name = remote_config.default_host_name()
        cleared = True
    else:
        new_name = remote_config.sanitize_display_name(
            str(getattr(args, "name", "") or ""), max_len=80, fallback=""
        )
        cleared = False
    if not new_name:
        return _fail(t("remote.rename.empty"), args.json,
                     code="usage_error", exit_code=EXIT_USAGE)
    if dry_run:
        data = {"host_name": new_name, "cleared": cleared, "changed": False,
                "dry_run": True}
        if args.json:
            print(_envelope(True, data))
        else:
            print(t("remote.dry_run.banner"))
            if cleared:
                print(t("remote.dry_run.rename_cleared", name=new_name))
            else:
                print(t("remote.dry_run.rename", name=new_name))
        return EXIT_OK
    changed = state.host_name != new_name
    state.host_name = new_name
    remote_config.save_state(state)
    if cleared:
        message = t("remote.rename.cleared", default_name=state.host_name)
    else:
        message = t("remote.rename.done", name=state.host_name)
    data = {"host_name": state.host_name, "cleared": cleared, "changed": changed}
    if args.json:
        print(_envelope(True, data))
    else:
        print(message)
    return EXIT_OK


def _cmd_rotate_key(args) -> int:
    state = remote_config.load_state()
    if _is_dry_run(args):
        # 演练：不轮换密钥、不重新注册。轮换天生非幂等（每次都是新密钥），
        # dry-run 是唯一的 rehearsal 手段。
        data = {"rotated": False, "host_id": state.host_id, "dry_run": True}
        if args.json:
            print(_envelope(True, data))
        else:
            print(t("remote.dry_run.banner"))
            print(t("remote.dry_run.rotate"))
        return EXIT_OK
    remote_config.rotate_host_key(state)
    from corral.remote import account as remote_account

    ok, message = remote_account.register_host(state)
    if not ok:
        return _fail(message, args.json, code="account_error",
                     next_commands=[f"{_bin()} remote status --json"])
    text = t("remote.rotate.done")
    if args.json:
        print(_envelope(True, {"rotated": True, "host_id": state.host_id}))
    else:
        print(text)
    return EXIT_OK


def _cmd_login(args) -> int:
    state = remote_config.load_state()
    relay = args.relay_url or state.relay_url
    from corral.remote import account as remote_account

    ok, message = remote_account.login(relay)
    if not ok:
        return _fail(message, args.json, code="account_error",
                     next_commands=[f"{_bin()} remote status --json"])
    print(_envelope(True, remote_account.whoami()) if args.json else message)
    return EXIT_OK


def _cmd_logout(args) -> int:
    from corral.remote import account as remote_account

    message = remote_account.logout()
    print(_envelope(True, {"logged_out": True}) if args.json else message)
    return EXIT_OK


def _cmd_whoami(args) -> int:
    from corral.remote import account as remote_account

    info = remote_account.whoami()
    if args.json:
        print(_envelope(True, info))
        return EXIT_OK
    if not info.get("login"):
        print(t("remote.status.account_none"))
        return EXIT_OK
    print(t("remote.status.account", login=info.get("login")))
    return EXIT_OK


# 兼容旧名：行为已切到开关语义。
_cmd_start = _cmd_on
_cmd_stop = _cmd_off


# ---------------------------------------------------------------------------

def _add_service_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--relay-url", help=t("remote.help.relay_url"))
    parser.add_argument(
        "--insecure-relay",
        action="store_true",
        help=t("remote.help.insecure_relay"),
    )
    parser.add_argument("--no-relay", action="store_true", help=t("remote.help.no_relay"))
    parser.add_argument("--no-local", action="store_true", help=t("remote.help.no_local"))
    parser.add_argument("--port", type=int, help=t("remote.help.port"))
    parser.add_argument(
        "--force",
        action="store_true",
        help=t("remote.help.force"),
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help=t("remote.help.foreground"),
    )
    parser.add_argument("--quiet", action="store_true", help=t("remote.help.quiet"))
    parser.add_argument("--dry-run", action="store_true", help=t("remote.help.dry_run"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corral remote",
        description=t("remote.cli.description"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=t("remote.cli.epilog"),
    )
    sub = parser.add_subparsers(dest="command")

    on = sub.add_parser("on", help=t("remote.help.on"))
    _add_service_flags(on)
    on.set_defaults(func=_cmd_on)

    # 兼容旧名；help 写明已是开关别名。
    start = sub.add_parser("start", help=t("remote.help.start"))
    _add_service_flags(start)
    start.set_defaults(func=_cmd_on)

    off = sub.add_parser("off", help=t("remote.help.off"))
    off.add_argument("--dry-run", action="store_true", help=t("remote.help.dry_run"))
    off.set_defaults(func=_cmd_off)

    stop = sub.add_parser("stop", help=t("remote.help.stop"))
    stop.add_argument("--dry-run", action="store_true", help=t("remote.help.dry_run"))
    stop.set_defaults(func=_cmd_off)

    serve = sub.add_parser("_serve", help=argparse.SUPPRESS)
    serve.set_defaults(func=_cmd_serve)

    pair = sub.add_parser("pair", help=t("remote.help.pair"))
    pair.add_argument(
        "--readonly",
        action="store_true",
        help=t("remote.help.readonly"),
    )
    pair.add_argument("--dry-run", action="store_true", help=t("remote.help.dry_run"))
    pair.set_defaults(func=_cmd_pair)

    status = sub.add_parser("status", help=t("remote.help.status"))
    status.set_defaults(func=_cmd_status)

    devices = sub.add_parser("devices", help=t("remote.help.devices"))
    devices.set_defaults(func=_cmd_devices)

    unpair = sub.add_parser("unpair", help=t("remote.help.unpair"))
    unpair.add_argument("device_id")
    unpair.add_argument("--dry-run", action="store_true", help=t("remote.help.dry_run"))
    unpair.set_defaults(func=_cmd_unpair)

    rotate = sub.add_parser("rotate-key", help=t("remote.help.rotate_key"))
    rotate.add_argument("--dry-run", action="store_true", help=t("remote.help.dry_run"))
    rotate.set_defaults(func=_cmd_rotate_key)

    rename = sub.add_parser("rename", help=t("remote.help.rename"))
    rename.add_argument("name", nargs="?", default="",
                          help=t("remote.help.rename_value"))
    rename.add_argument("--clear", action="store_true",
                        help=t("remote.help.rename_clear"))
    rename.add_argument("--dry-run", action="store_true", help=t("remote.help.dry_run"))
    rename.set_defaults(func=_cmd_rename)

    login = sub.add_parser("login", help=t("remote.help.login"))
    login.add_argument("--relay-url", help=t("remote.help.relay_url"))
    login.set_defaults(func=_cmd_login)

    logout = sub.add_parser("logout", help=t("remote.help.logout"))
    logout.set_defaults(func=_cmd_logout)

    whoami = sub.add_parser("whoami", help=t("remote.help.whoami"))
    whoami.set_defaults(func=_cmd_whoami)

    for action in (on, start, off, stop, pair, status, devices, unpair, rotate, rename, login, logout, whoami):
        action.add_argument("--json", action="store_true", help=t("remote.help.json"))
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    return args.func(args)
