"""corral 命令行入口：参数解析、直启分发、TUI 启动与外层接管。"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass

# Kitty 键盘协议的关闭开关已上移到 `corral/__init__.py`（包顶层，任何用法必经），
# 这里不再重复设置：本模块的 `from corral import ...` 一定先执行包初始化。
from corral import agent_api, cursor_observer, embed, keepalive, observe, titles, updater
from corral import theme as theme_mod
from corral.i18n import t
from corral.legacy_names import getenv, pop_env, setenv_primary
from corral.models import LaunchPlan, LaunchRequest, NewSessionRequest
from corral.runtime import LaunchError, RuntimeRegistry, default_registry, execute_launch
from corral.store import SessionStore
from corral.theme import _probe_osc_colours


def _restart_process() -> None:
    """客户端自动更新：升级成功、用户点了「重启」后，用新装好的磁盘代码
    re-exec 一个全新 corral 进程，原样透传本次启动的命令行参数。

    tmux 保活会话与本进程无关，重启不影响已托管会话；execv 成功后本进程
    直接被替换，不会返回。

    优先用 PATH 上的 corral 可执行文件重启：Homebrew 升级把新版本装进新的
    Cellar/venv 并把 bin/corral 软链指向它，而 sys.executable 仍指向旧版本
    Cellar 里那个冻结的解释器——用它 re-exec 只会重新加载旧代码，用户点了
    「重启」却还是旧版本。找不到 PATH 上的 corral（裸模块 / 源码运行）时，
    再退回当前解释器的 `-m corral`。"""

    sys.stdout.flush()
    sys.stderr.flush()
    exe = shutil.which("corral")
    if exe:
        os.execv(exe, [exe, *sys.argv[1:]])
        return  # execv 正常不会返回；防御性兜底，避免万一继续往下走
    os.execv(sys.executable, [sys.executable, "-m", "corral", *sys.argv[1:]])


def _finish_self_update(request: updater.RestartRequest) -> bool:
    """界面退出后执行升级；成功时用新安装自动替换当前进程。"""
    from corral.i18n import t

    print(t("update.cli_updating", version=request.latest))
    ok, output = updater.run_update(request.latest, request.channel)
    observe.event(
        "self_update", ok=ok, latest=request.latest, channel=request.channel
    )
    if output:
        print(output)
    if not ok:
        observe.debug("self_update_output", output=output)
        print(t("update.cli_failed"), file=sys.stderr)
        return False
    print(t("update.cli_updated", version=request.latest))
    _restart_process()
    return True

@dataclass(frozen=True)
class _DirectLaunch:
    """直启子命令（`corral claude …`）带进 TUI 的待托管启动计划：

    进入主循环前就把新会话托管进保活 socket 并聚焦右栏，让直启与界面内
    「新建会话」走完全相同的内嵌路径。
    """

    plan: LaunchPlan
    runtime_id: str
    ident: str


def _launch(request: LaunchRequest | NewSessionRequest, registry: RuntimeRegistry, keepalive_on: bool) -> None:
    """生成启动计划并让目标运行时接管当前终端。

    会话已经在后台保活时直接接回现场，不重新拉起一个和它竞争同一份会话文件的
    新进程；否则按 keepalive_on 开关决定新启动的进程要不要包进保活层。
    """
    if isinstance(request, LaunchRequest):
        # force_new / copy_session = 新会话，不能 attach 到原会话的保活窗。
        if not request.force_new and not request.copy_session:
            attach = keepalive.attach_plan(request.session)
            if attach is not None:
                execute_launch(attach)
                return
        plan = registry.build_launch_plan(request)
        native_resume = (
            request.session.get("source") == request.target_runtime_id
            and not request.force_new
            and not request.copy_session
        )
        ident = request.session["id"] if native_resume else keepalive.new_session_ident()
    else:
        plan = registry.build_new_session_plan(request)
        ident = keepalive.new_session_ident()

    if keepalive_on:
        plan = keepalive.wrap_plan(plan, request.target_runtime_id, ident)
    execute_launch(plan)


def _format_resume_command(argv: tuple[str, ...]) -> str:
    """把启动计划的 argv 拼成可直接在 shell 中运行的命令字符串。"""
    parts = []
    for arg in argv:
        if " " in arg or "\n" in arg or '"' in arg:
            escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'"{escaped}"')
        else:
            parts.append(arg)
    return " ".join(parts)


def _output_json(registry, limit: int) -> None:
    """以 JSON 格式输出所有运行时的会话列表，每条附上恢复命令，然后退出。

    供大模型或自动化脚本调用：不启动 TUI，不触发后台标题生成，
    不消耗 Claude 额度。标题使用本地临时兜底标题（fallback_title）。

    兼容保留的扁平数组形状（无 envelope、无状态枚举）；新集成请用 `corral list`。
    """
    scanned = registry.scan_all(limit)
    result = []
    for runtime in registry:
        for session in scanned.get(runtime.id, []):
            try:
                plan = runtime.build_resume_plan(session)
                resume_cmd = _format_resume_command(plan.argv)
            except Exception:
                resume_cmd = None
            result.append({
                "runtime": session.get("source"),
                "id": session.get("id"),
                "title": session.get("fallback_title") or session.get("native_title") or "",
                "cwd": session.get("cwd") or "",
                "time": session.get("display_time") or "",
                "mtime": session.get("mtime"),
                "size_kb": round(session.get("size_kb") or 0, 1),
                "status": session.get("status_tag") or "",
                "resume_command": resume_cmd,
                "history_path": session.get("path") or "",
            })
    print(json.dumps(result, ensure_ascii=False, indent=2))


_TITLE_LOCK_FILE = os.path.join(titles.CACHE_DIR, "titles.lock")
# 生成耗时期间又冒出的会话：解锁前最多再扫几轮，避免「只扫一次就退出」漏生成。
_TITLE_DRAIN_MAX_ROUNDS = 3


def _collect_title_pending(scanned: dict, cache: dict) -> list[dict]:
    pending = []
    for bucket in scanned.values():
        for session in bucket:
            _, needs = titles.resolve_initial_title(session, cache)
            if needs:
                pending.append(session)
    return pending


def _run_title_daemon(registry: RuntimeRegistry, limit: int) -> None:
    """脱离 TUI 的独立标题生成进程入口（corral --generate-titles）。

    用文件锁保证全机单实例：拿不到锁说明已有后台进程在跑，直接退出，
    避免用户反复进 corral 堆积多个生成进程、重复消耗模型额度。
    每轮生成后再扫一遍 pending，最多 drain 若干轮后退出。
    """
    os.makedirs(titles.CACHE_DIR, exist_ok=True)
    lock_fp = open(_TITLE_LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # 已有进程持锁，本次无需重复生成

    try:
        for _ in range(_TITLE_DRAIN_MAX_ROUNDS):
            scanned = registry.scan_all(limit)
            cache = titles.load_cache()
            pending = _collect_title_pending(scanned, cache)
            if not pending:
                break
            titles.refresh_titles(pending, cache)
    finally:
        fcntl.flock(lock_fp, fcntl.LOCK_UN)
        lock_fp.close()


def _title_daemon_running() -> bool:
    """True when another process holds the title lock (cheap probe, no fork).

    Callers spawn a whole interpreter that imports the registry only to lose the
    lock and exit; probing first skips that fork while a generator is running. A
    shared lock never blocks other probers; any error reads as "not running" so
    the spawn proceeds exactly as before (the daemon still guards itself).
    """
    try:
        fd = os.open(_TITLE_LOCK_FILE, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _spawn_title_daemon(limit: int) -> None:
    """以脱离当前终端的方式拉起后台标题生成进程。

    start_new_session 让子进程独立成新会话/进程组：TUI 之后无论被 execvp
    替换（原生恢复）还是退出，该进程都继续把标题生成完并写入缓存。
    已有进程持锁时直接跳过，不再白白起一个必然撞锁退出的解释器。
    """
    if _title_daemon_running():
        return
    try:
        subprocess.Popen(
            [sys.executable, "-m", "corral", "--generate-titles", "--limit", str(limit)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass  # 拉起失败仅退化为「只显示临时兜底标题」，不影响主流程


def _require_tmux() -> None:
    """corral 的会话托管/内嵌面板/断线保活全部建立在 tmux 之上，属于硬依赖；
    缺失或版本过旧时明确报错并给出安装/升级提示，不静默降级出残废功能。

    版本下限检查复用 embed._tmux_version()：`new-session -e` 环境变量注入
    （托管会话的 CORRAL_RUNTIME/CORRAL_SESSION_ID 等元数据唯一注入点）和
    pause-after 流控通知都要求 tmux 3.2+，更旧版本上创建托管会话会直接失败，
    报出的却是一个和版本无关的笼统 EmbedError，用户很难联想到升级 tmux。
    版本解析不出（如 `tmux -V` 输出被魔改）时不阻断——宁可信任已通过的
    `shutil.which` 探测，让真实失败在后续调用里自然暴露，不做过度拦截。"""
    import corral as pkg

    if pkg.shutil.which("tmux") is None:
        print(t("error.tmux_missing"), file=pkg.sys.stderr)
        pkg.sys.exit(1)
    version = pkg.embed._tmux_version()
    if version is not None and version < pkg.embed.MIN_TMUX_VERSION:
        need_major, need_minor = pkg.embed.MIN_TMUX_VERSION
        print(
            t(
                "error.tmux_old",
                need=f"{need_major}.{need_minor}",
                current=f"{version[0]}.{version[1]}",
            ),
            file=pkg.sys.stderr,
        )
        pkg.sys.exit(1)


# 直启子命令进 TUI 侧边栏模式时的每运行时扫描深度，与主 TUI 的 --limit 默认值一致
_DIRECT_LAUNCH_LIMIT = 50


def _dispatch_direct_launch(argv: list[str], registry: RuntimeRegistry) -> None:
    """处理 `corral [--no-keepalive] <runtime> [参数…]` 直启子命令。

    分流：
    - `corral claude subswap`：第二个位置参数不以 `-` 开头 → 项目名模糊匹配，
      在匹配目录下 `build_new_session_plan`（新建空白会话）。
    - `corral claude --resume …` / 无额外参数：透传（`build_passthrough_plan` 只垫
      默认全自动放行参数，用户已显式带了就不重复；放行参数的位置与适用子命令有
      讲究的助手（OpenCode 的 `--auto`）由适配器的 `compose_passthrough_argv` 决定）。

    真实终端且内嵌可用时默认进入 TUI 侧边栏模式；非真实终端、`--no-keepalive`
    或内嵌不可用时保持直接启动。经 `import corral as pkg` 取符号，便于测试 patch。
    """
    import corral as pkg
    from corral import projects as projects_mod

    pkg._require_tmux()
    no_keepalive = argv and argv[0] == "--no-keepalive"
    rest = argv[1:] if no_keepalive else argv
    # 别名（`agent` / `cursor-agent` → `cursor`）只在这里展开一次，后续全用 id。
    runtime_id, user_args = registry.resolve_id(rest[0]) or rest[0], rest[1:]

    def _is_subcommand(token: str) -> bool:
        """`corral <运行时> run …` 这类首个词是子命令的直启属于透传，不是项目快捷启动。

        只有 OpenCode 有子命令形态（见 `OpenCodeRuntime.SUBCOMMANDS`）；其余运行时
        一律按旧语义（首个位置参数是项目名）。
        """
        if runtime_id != "opencode":
            return False
        from corral.runtime import opencode as runtime_opencode

        return token in runtime_opencode.OpenCodeRuntime.SUBCOMMANDS

    project_query: str | None = None
    if (
        user_args
        and not user_args[0].startswith("-")
        and not _is_subcommand(user_args[0])
    ):
        project_query = user_args[0]
        if user_args[1:]:
            print(
                f"项目快捷启动不接受额外参数：{user_args[1:]!r}。"
                "透传请让参数以 - 开头（如 corral claude --resume id）。",
                file=pkg.sys.stderr,
            )
            pkg.sys.exit(1)

    tty = bool(pkg.sys.stdin.isatty() and pkg.sys.stdout.isatty())
    use_tui = tty and pkg.embed.available(no_keepalive)
    store = None

    if use_tui:
        pkg.keepalive.reap()
        store = pkg.SessionStore(limit=_DIRECT_LAUNCH_LIMIT, registry=registry)
        store._title_spawn_fn = pkg._spawn_title_daemon
        store.load()
        pkg._spawn_title_daemon(_DIRECT_LAUNCH_LIMIT)

    if project_query is not None:
        try:
            if store is not None:
                discovered = projects_mod.discover(
                    projects_mod.session_cwds_from_sessions(store.sessions),
                )
            else:
                scanned = registry.scan_all(_DIRECT_LAUNCH_LIMIT)
                discovered = projects_mod.discover(
                    projects_mod.session_cwds_from_sessions(scanned),
                )
            cwd = projects_mod.resolve_query(project_query, discovered)
            plan = registry.get(runtime_id).build_new_session_plan(cwd)
        except projects_mod.ProjectResolveError as exc:
            print(str(exc), file=pkg.sys.stderr)
            pkg.sys.exit(1)
        except pkg.LaunchError as exc:
            print(t("error.launch_failed", error=exc), file=pkg.sys.stderr)
            pkg.sys.exit(1)
    else:
        plan = registry.build_passthrough_plan(runtime_id, user_args)

    ident = pkg.keepalive.new_session_ident()

    if not use_tui:
        if pkg.keepalive.enabled(no_keepalive):
            plan = pkg.keepalive.wrap_plan(plan, runtime_id, ident)
        try:
            pkg.execute_launch(plan)
        except pkg.LaunchError as exc:
            print(t("error.launch_failed", error=exc), file=pkg.sys.stderr)
            pkg.sys.exit(1)
        return

    # 与 main() 的 TUI 入口相同：趁 Textual 接管终端前探测外层终端前景/背景色。
    theme_mod._OSC_REPORT = pkg._probe_osc_colours(timeout=0.25)

    from corral.ui.app import restore_terminal, run_app
    try:
        chosen = run_app(store, True, _DirectLaunch(plan, runtime_id, ident), theme_mod._OSC_REPORT)
    except KeyboardInterrupt:
        restore_terminal()
        chosen = None
    pkg.embed.close_channel()
    if isinstance(chosen, pkg.updater.RestartRequest):
        if not pkg._finish_self_update(chosen):
            pkg.sys.exit(1)
        return
    if chosen is None:
        return
    try:
        pkg._launch(chosen, store.registry, pkg.keepalive.enabled(no_keepalive))
    except pkg.LaunchError as exc:
        print(t("error.launch_failed", error=exc), file=pkg.sys.stderr)
        pkg.sys.exit(1)


def main() -> None:
    # Cursor hook 是最轻量、最保守的入口：只消费 stdin 中的一次 JSON 事件。
    # 任何格式或落盘异常都只返回一行空 JSON 对象和退出码 0，既满足 Cursor 的
    # hook 响应契约，也绝不回显事件正文或阻断 Cursor 自身。
    if len(sys.argv) > 1 and sys.argv[1] == "_cursor-hook":
        try:
            payload = json.load(sys.stdin)
            if isinstance(payload, dict):
                cursor_observer.ingest(payload.get("hook_event_name"), payload)
        except Exception:
            pass
        print("{}")
        sys.exit(0)

    # 观察器管理命令有独立的结构化输出、退出码与 dry-run 契约，不进入 TUI、
    # tmux 或只读会话数据接口。
    if len(sys.argv) > 1 and sys.argv[1] == "observer":
        sys.exit(cursor_observer.cli_main(sys.argv[2:]))

    # 尽早挂崩溃钩子：TUI 闪退后 stderr 常被清掉，必须先落盘才能事后 diagnose。
    observe.install_crash_hooks()
    # 活进程诊断：TUI 收到 SIGUSR1 时把全线程栈追加到 stacks.log（kill -USR1 取栈）。
    observe.install_stack_dumps()

    # list/search/show/export/share/context/plan/describe 是面向 Agent 的机器可读子命令，整体转发给
    # agent_api，不与下面的 TUI/--json 旧参数共用同一个 parser。
    if len(sys.argv) > 1 and sys.argv[1] in agent_api.COMMAND_ROOT_NAMES:
        sys.exit(agent_api.dispatch(sys.argv[1:]))

    # `corral update`：手动触发客户端自动更新，不放进只读的 agent_api。
    if len(sys.argv) > 1 and sys.argv[1] == "update":
        sys.exit(updater.cli_update())

    # `corral claude …` / `corral codex …`（可选前置 --no-keepalive）是直启透传子命令，同样整体
    # 绕开下面的 TUI/--json 旧参数 parser；分发探测此处只需运行时 ID 集合。
    _direct_launch_argv = sys.argv[1:]
    _direct_launch_probe = (
        _direct_launch_argv[1:] if _direct_launch_argv[:1] == ["--no-keepalive"] else _direct_launch_argv
    )
    _direct_launch_registry = default_registry()
    if _direct_launch_probe and _direct_launch_probe[0] in _direct_launch_registry.launch_tokens:
        _dispatch_direct_launch(_direct_launch_argv, _direct_launch_registry)
        return

    parser = argparse.ArgumentParser(
        description=t("cli.help.description"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=t("cli.help.epilog"),
    )
    parser.add_argument("--limit", type=int, default=50, help=t("cli.help.limit"))
    parser.add_argument("--json", action="store_true", dest="json_mode",
                        help=t("cli.help.json"))
    parser.add_argument("--no-input", action="store_true", dest="no_input",
                        help=t("cli.help.no_input"))
    parser.add_argument("--no-keepalive", action="store_true", dest="no_keepalive",
                        help=t("cli.help.no_keepalive"))
    parser.add_argument("--no-color", action="store_true", dest="no_color",
                        help=t("cli.help.no_color"))
    parser.add_argument("-d", "--debug", "--verbose", action="store_true", dest="debug",
                        help=t("cli.help.debug"))
    parser.add_argument("-q", "--quiet", action="store_true", dest="quiet",
                        help=t("cli.help.quiet"))
    parser.add_argument("--version", "-V", "-v", action="store_true", dest="show_version",
                        help=t("cli.help.version"))
    parser.add_argument("--generate-titles", action="store_true", dest="generate_titles",
                        help=argparse.SUPPRESS)  # 内部用途：TUI 拉起的后台标题生成进程
    args = parser.parse_args()

    # 通用 CLI 开关先于任何输出和 TUI 导入生效；quiet 与 debug 同时出现时以 quiet 为准。
    if args.no_color:
        os.environ["NO_COLOR"] = "1"
    if args.quiet:
        pop_env("DEBUG")
    elif args.debug:
        setenv_primary("DEBUG", "1")

    registry = default_registry()

    if args.show_version:
        report = updater.install_report()
        print(f"corral {report['version']}")
        print(f"  package_file: {report['package_file']}")
        print(f"  python:       {report['python']}")
        print(f"  channel:      {report['channel']}")
        if report["checkout_root"]:
            print(f"  checkout:     {report['checkout_root']}")
            print(f"  from_checkout:{report['loaded_from_checkout']}")
        if report["stale_source_warning"]:
            print(f"  WARNING:      {report['stale_source_warning']}", file=sys.stderr)
        return

    if args.generate_titles:
        _run_title_daemon(registry, args.limit)
        return

    if args.json_mode or args.no_input:
        # 显式 legacy 路径：stderr 给一句迁移提示，stdout 保持纯数组可解析。
        print(t("cli.json.legacy_hint"), file=sys.stderr)
        _output_json(registry, args.limit)
        return

    # 没有真实终端（管道、脚本、被 Agent 直接调用）时，Textual 无法接管终端；自动
    # 退化为 JSON 列表而不是崩溃。stdin/stdout 分开检测：任一端不是真实终端都不能进 TUI。
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _output_json(registry, args.limit)
        return

    _require_tmux()

    keepalive_on = keepalive.enabled(args.no_keepalive)
    if keepalive_on:
        keepalive.reap()  # 空闲超时 + 超软上限时压掉闲置非执行中会话，不常驻额外进程

    store = SessionStore(limit=args.limit, registry=registry)
    store._title_spawn_fn = _spawn_title_daemon
    # 先同步读上次退出前的侧边栏快照（几十毫秒），首帧直接带卡；必须在下面的
    # 后台加载线程启动前完成，否则会被真扫描的合并覆盖。真扫描照常跑，完成后经
    # 原地更新/区段 splice 收敛到最新状态（运行状态/标题可能滞后一两秒，可接受）。
    from corral.cache import enabled as _cache_enabled

    if _cache_enabled():
        store.hydrate_from_snapshot()
    # store.load()（磁盘扫描 + JSON 解析）和下面的 _probe_osc_colours()（终端 OSC
    # 10/11 探测，最长阻塞约 0.25s）互不依赖。无快照时仍提前开扫，与 OSC 重叠；
    # 有快照时推迟到首帧画完再扫（MainScreen._begin_deferred_load），避免六个解析
    # 线程与 Textual 首铺抢同一把解释器锁——忙时曾把首帧卡到数秒白屏。
    # 找不到任何会话不再在这里直接 sys.exit(1)：扫描本身现在是异步的，主进程无法
    # 同步判断"扫完了但真的一条都没有"，这个空状态提示改由 MainScreen 在
    # wait_loaded() 完成后展示（见 ui/main_screen.py 的 _update_header）。
    if store.hydrated:
        store._load_deferred = True
    else:
        threading.Thread(target=store.load, daemon=True).start()

    # 拉起脱离终端的后台进程生成标题：用户秒退或原生恢复（execvp 替换进程）后仍继续，
    # TUI 通过轮询缓存文件拾取它逐批写入的标题。
    _spawn_title_daemon(args.limit)

    # 趁 Textual 接管终端前探测外层终端的前景/背景色（OSC 10/11）：内嵌面板聚焦时经
    # refresh-client -r 注入托管 pane，让 pane 内 agent 的深/浅主题检测拿到真实值。
    # 这一步仍然必须在 UI 启动前同步完成（EmbedPane/MainScreen 的深浅色主题判断
    # 依赖它），但现在跟上面的后台扫描线程是并行的，不再是"扫描 + 探测"首尾相加。
    # 读到应答即返回（快终端只需数毫秒，不受上限影响）；上限给足 0.25s 以覆盖
    # tmux/SSH 的往返，确保应答在 Textual 接管前收完——上限太短会让晚到的应答漏进
    # TUI 被当键盘输入注入搜索框（搜索框乱码 + 会话列表被过滤空）。不应答的终端最
    # 多白等 0.25s，且与上面的后台扫描线程并行，通常被扫描耗时掩盖。
    theme_mod._OSC_REPORT = _probe_osc_colours(timeout=0.25)
    observe.init(debug=bool(getenv("DEBUG")))
    # 在源码树里开发却加载了 pipx/site-packages 副本时，启动前打一次 stderr 告警
    # （普通发行版用户 cwd 不在仓库内，不会触发）。
    stale = updater.stale_source_warning()
    if stale and sys.stderr.isatty() and not args.quiet:
        print(f"[corral] {stale}", file=sys.stderr)
    if getenv("DEBUG"):
        observe.debug(
            "osc_probe",
            report_present=theme_mod._OSC_REPORT is not None,
            in_tmux=bool(os.environ.get("TMUX")),
            theme_report=embed.supports_theme_report(),
        )
        print(f"[corral debug] 外层终端 OSC 10/11 探测: {theme_mod._OSC_REPORT!r} "
              f"(tmux={'是' if os.environ.get('TMUX') else '否'}, "
              f"refresh -r 支持={'是' if embed.supports_theme_report() else '否'})",
              file=sys.stderr)

    # 系统忙时默认调度等级会被浏览器 / IDE 饿死，TUI 表现为「自己不重却卡」。
    # 进入交互界面前把主线程抬到用户交互优先级（抓帧线程在各自入口自抬）。
    from corral.schedprio import boost_interactive

    boost_interactive()
    from corral.ui.app import restore_terminal, run_app
    try:
        chosen = run_app(store, embed.available(args.no_keepalive), osc_report=theme_mod._OSC_REPORT)
    except KeyboardInterrupt:
        # run_app 已吞一次；若收尾阶段再次被打断，仍恢复终端、不当成崩溃。
        restore_terminal()
        chosen = None
    # 兜底关闭内嵌控制通道：pane 聚焦时打开的 `tmux -C attach` 控制 client 只有
    # c 键关分栏才会关，Esc 退出/回车全屏接管等退出路径不经那条分支——不在这里统一
    # 兜底就会把孤儿控制 client 留在保活服务端上。close_channel 无通道时是空操作。
    embed.close_channel()
    if isinstance(chosen, updater.RestartRequest):
        if not _finish_self_update(chosen):
            sys.exit(1)
        return
    if chosen is None:
        return

    try:
        _launch(chosen, store.registry, keepalive_on)
    except LaunchError as exc:
        print(t("error.launch_failed", error=exc), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
