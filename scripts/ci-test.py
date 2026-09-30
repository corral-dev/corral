#!/usr/bin/env python3
"""CI 用的检查入口：先 lint，再跑全量 unittest，并解决两个只在 CI 上要命的问题。

**〇、lint 必须和 CI 同源，不能只跑单测。**
GitHub Actions 在单测前先跑 `ruff check src tests`（固定 `ruff==0.16.1`）。
以前本脚本只管 unittest，发版机只跑 `ci-test.py` 会绿、推上去却在 Lint
步全矩阵报红——2026-08-07 起 `v0.24.57`～`v0.24.65` 每次推送都因
`tests/test_cache.py` 一处 import 排序（I001）发失败邮件，单测根本没跑到。
把 ruff 收进本入口后，本机绿 ≈ CI 绿。

**一、挂死要能自曝位置，不能干等到作业上限。**
2026-07-30 macOS runner 上真实发生过：单测跑到一半卡住，GitHub 作业没有配
timeout，于是整整占着 runner 6 小时直到被平台按上限杀掉。免费额度的 macOS
并发本来就少，两个这样的僵尸作业能把后面所有排队任务拖到十几小时，连带一片
「cancelled」。这里到点用 faulthandler 把**所有线程的栈**打出来再退出——日志
里直接能看到卡在哪个用例的哪一行，而不是只剩一句「The operation was canceled」。

**二、已知的 Textual Pilot 偶发不该变成失败邮件。**
`AGENTS.md` 与 `docs/MAINTAINER_GUIDE.md` 早就写明：涉及真实 tmux 回显与 Pilot
等待的用例在负载高的机器上会假失败，判定方法是**把失败用例单独重跑**。这段
判断以前只写在文档里靠人执行，CI 仍旧一失败就发邮件。这里把它固化下来：失败
用例自动单独重跑一次，两次都失败才算真回归。真回归是确定性的，重跑照样挂，
不会被这层重试掩盖；而单次偶发（实测 `test_focusing_split_pane_highlights_
matching_sidebar_session` 约十次一遇）不再污染 CI 结论。

**三、按模块并行，但不拆界面/终端集成、也不跳用例。**
完整套件的墙钟时间几乎都在 `test_ui`（Textual Pilot + 真实 tmux）。其它模块
可以和这条串行车道重叠跑：多进程按**模块**并行，碰共享保活 socket / Pilot
的模块仍进同一条串行车道，避免互相抢 `tmux -L corral-keepalive`。禁止用「只跑
改过的文件」或跳过界面集成来假装发版门禁变快。``CORRAL_TEST_JOBS=1`` 可退回
旧的单进程全量顺序。

用法与 CI 的 Lint+Test 两步等价，退出码同语义。

``--lint-only``：只跑 ruff（推送前门禁，几秒级）；完整入口留给发版推送与
``publish-release.sh``。

``--check-stamp``：本工作区产品代码是否刚跑过完整检查（给推送门禁和收尾脚本
用来跳过重复，不跑 lint/单测）。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import faulthandler
import json
import os
import subprocess
import sys
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

# Match corral package default before any test module can import textual.
# Some serial-lane tests historically imported textual before corral; workers
# and the in-process retry path must still freeze DISABLE_KITTY_KEY correctly.
os.environ.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import ci_stamp  # noqa: E402

ROOT = _SCRIPTS_DIR.parent
_SRC = str(ROOT / "src")
# 本机 `python3` 常常没有装过 corral；子进程 `python -m corral` 也要找得到包。
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
_existing = os.environ.get("PYTHONPATH", "")
if _SRC not in _existing.split(os.pathsep):
    os.environ["PYTHONPATH"] = _SRC if not _existing else _SRC + os.pathsep + _existing

# 单个作业的硬上限（秒）。取值要明显小于 CI 作业自身的 timeout-minutes，
# 才能保证「先由我们打出栈」而不是「先被平台静默杀掉」。
HANG_DUMP_SECONDS = int(os.environ.get("CORRAL_TEST_HANG_SECONDS", "1500"))
# 与 `.github/workflows/test.yml` 的 Lint 步保持同一版本，避免规则集漂移。
RUFF_VERSION = "0.16.1"

# 子进程回报行前缀（stdout 最后一行）。
_RESULT_PREFIX = "CORRAL_CI_TEST_RESULT:"

# 共享真实 tmux 保活 socket，或驱动 Textual Pilot 的模块：彼此串行，
# 但可以与其它纯单测模块并行。
_SERIAL_MODULES = frozenset(
    {
        "test_ui",
        "test_embed",
        "test_attention_ui",
        "test_dragon_easter_egg",
        "test_dragon_splash",
        "test_update_toast",
        "test_main_screen_update",
        "test_tui_sidebar_review",
        "test_tui_preview_review",
        "test_tui_embed_review",
    }
)


@dataclass(frozen=True)
class _ShardResult:
    modules: tuple[str, ...]
    ok: bool
    failed_ids: tuple[str, ...]
    tests_run: int
    seconds: float
    output: str
    returncode: int


def _collect_ids(result: unittest.TestResult) -> list[str]:
    """取出本轮失败/出错的用例 id，用于精确重跑。"""
    ids: list[str] = []
    for test, _ in list(result.failures) + list(result.errors):
        test_id = getattr(test, "id", None)
        if test_id is None:
            return []  # 拿不到 id（如加载期错误）就别重跑，直接判失败
        ids.append(test_id())
    return ids


def _run_ruff() -> int:
    """跑与 CI 相同的 ruff 检查；优先 PATH / `python -m ruff`，不硬装系统包。"""
    candidates = (
        ["ruff"],
        [sys.executable, "-m", "ruff"],
    )
    for prefix in candidates:
        probe = subprocess.run(
            [*prefix, "--version"],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0 and RUFF_VERSION in (probe.stdout or ""):
            print("=== ruff check src tests ===")
            return subprocess.run([*prefix, "check", "src", "tests"]).returncode
    print(
        f"错误：未找到 ruff=={RUFF_VERSION}（与 CI Lint 步一致）。\n"
        f"  macOS: brew install ruff\n"
        f"  其它:  python3 -m pip install ruff=={RUFF_VERSION}",
        file=sys.stderr,
    )
    return 1


def _discover_modules() -> list[str]:
    return sorted(path.stem for path in (ROOT / "tests").glob("test_*.py"))


def _default_jobs() -> int:
    raw = os.environ.get("CORRAL_TEST_JOBS")
    if raw is not None and raw.strip() != "":
        try:
            return max(1, int(raw))
        except ValueError:
            print(
                f"错误：CORRAL_TEST_JOBS 必须是整数，收到 {raw!r}",
                file=sys.stderr,
            )
            raise SystemExit(2) from None
    cpu = os.cpu_count() or 2
    # 至少 2：一条串行车道 + 至少一条并行；上限避免本机 16GB 机器被测爆。
    return max(2, min(cpu, 6))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="与 CI 同源的 lint + 单测入口")
    parser.add_argument(
        "--lint-only",
        action="store_true",
        help="只跑 ruff check（推送前门禁）；不加则再跑全量单测",
    )
    parser.add_argument(
        "--skip-lint",
        action="store_true",
        help="跳过 ruff（CI 矩阵已在独立 Lint 步跑过时用）",
    )
    parser.add_argument(
        "--check-stamp",
        action="store_true",
        help="只判断本工作区是否刚跑过完整检查（不跑 lint/单测）",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        metavar="N",
        help="并行进程数（默认 CORRAL_TEST_JOBS 或 min(CPU,6) 且≥2；1=旧单进程）",
    )
    parser.add_argument(
        "--run-modules",
        default=None,
        help=argparse.SUPPRESS,  # 内部：子进程跑逗号分隔的模块名
    )
    return parser.parse_args(argv)


def _record_success() -> None:
    ci_stamp.write_stamp(ROOT)
    print("=== 已记下完整检查戳（后续推送/收尾若产品代码未改则跳过重复）===")


def _ensure_tests_on_path() -> None:
    """与 ``discover(start_dir="tests")`` 一致：模块名是 ``test_foo``，不是 ``tests.test_foo``。"""
    tests_dir = str(ROOT / "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)


def _run_modules_in_process(module_names: list[str]) -> _ShardResult:
    """在当前进程跑若干 test_* 模块，供 --run-modules 子进程使用。"""
    import io

    faulthandler.dump_traceback_later(HANG_DUMP_SECONDS, exit=True)
    _ensure_tests_on_path()
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in module_names:
        suite.addTests(loader.loadTestsFromName(name))
    # 子进程输出由父进程转发；verbosity=2 与旧入口一致。
    buf = io.StringIO()
    runner = unittest.TextTestRunner(stream=buf, verbosity=2)
    started = time.perf_counter()
    result = runner.run(suite)
    seconds = time.perf_counter() - started
    failed = tuple(_collect_ids(result))
    return _ShardResult(
        modules=tuple(module_names),
        ok=result.wasSuccessful(),
        failed_ids=failed,
        tests_run=result.testsRun,
        seconds=seconds,
        output=buf.getvalue(),
        returncode=0 if result.wasSuccessful() else 1,
    )


def _worker_main(module_csv: str) -> int:
    modules = [part.strip() for part in module_csv.split(",") if part.strip()]
    if not modules:
        print(
            f"{_RESULT_PREFIX}"
            f"{json.dumps({'ok': True, 'failed_ids': [], 'tests_run': 0, 'seconds': 0.0, 'modules': []})}"
        )
        return 0
    try:
        shard = _run_modules_in_process(modules)
    except Exception as exc:  # noqa: BLE001 — worker must always emit the result line
        import traceback

        sys.stdout.write(traceback.format_exc())
        err_payload = {
            "ok": False,
            "failed_ids": [],
            "tests_run": 0,
            "seconds": 0.0,
            "modules": modules,
            "error": str(exc),
        }
        print(f"{_RESULT_PREFIX}{json.dumps(err_payload, ensure_ascii=False)}")
        return 1
    sys.stdout.write(shard.output)
    if not shard.output.endswith("\n"):
        sys.stdout.write("\n")
    payload = {
        "ok": shard.ok,
        "failed_ids": list(shard.failed_ids),
        "tests_run": shard.tests_run,
        "seconds": round(shard.seconds, 3),
        "modules": list(shard.modules),
    }
    print(f"{_RESULT_PREFIX}{json.dumps(payload, ensure_ascii=False)}")
    return shard.returncode


def _parse_worker_output(blob: str, modules: list[str], returncode: int) -> _ShardResult:
    lines = blob.splitlines()
    payload = None
    keep: list[str] = []
    for line in lines:
        if line.startswith(_RESULT_PREFIX):
            try:
                payload = json.loads(line[len(_RESULT_PREFIX) :])
            except json.JSONDecodeError:
                keep.append(line)
            continue
        keep.append(line)
    output = "\n".join(keep)
    if output and not output.endswith("\n"):
        output += "\n"
    if not isinstance(payload, dict):
        return _ShardResult(
            modules=tuple(modules),
            ok=False,
            failed_ids=(),
            tests_run=0,
            seconds=0.0,
            output=output or blob,
            returncode=returncode or 1,
        )
    failed = tuple(str(x) for x in payload.get("failed_ids") or ())
    return _ShardResult(
        modules=tuple(payload.get("modules") or modules),
        ok=bool(payload.get("ok")) and returncode == 0 and not failed,
        failed_ids=failed,
        tests_run=int(payload.get("tests_run") or 0),
        seconds=float(payload.get("seconds") or 0.0),
        output=output,
        returncode=returncode,
    )


def _spawn_shard(modules: list[str]) -> _ShardResult:
    if not modules:
        return _ShardResult((), True, (), 0, 0.0, "", 0)
    label = ",".join(modules)
    print(f"=== shard start ({len(modules)} module{'s' if len(modules) != 1 else ''}): {label} ===")
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--run-modules", label],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    blob = (proc.stdout or "") + (proc.stderr or "")
    shard = _parse_worker_output(blob, modules, proc.returncode)
    # 用父进程墙钟覆盖（含子进程启动开销），便于对照。
    shard = _ShardResult(
        modules=shard.modules,
        ok=shard.ok,
        failed_ids=shard.failed_ids,
        tests_run=shard.tests_run,
        seconds=time.perf_counter() - started,
        output=shard.output,
        returncode=shard.returncode,
    )
    sys.stdout.write(shard.output)
    status = "ok" if shard.ok else "FAIL"
    print(
        f"=== shard {status} in {shard.seconds:.1f}s "
        f"({shard.tests_run} tests): {label} ==="
    )
    return shard


def _run_suite_serial() -> tuple[bool, list[str]]:
    """旧路径：单进程 discover 全量。"""
    faulthandler.dump_traceback_later(HANG_DUMP_SECONDS, exit=True)
    os.chdir(ROOT)
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir="tests")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if result.wasSuccessful():
        return True, []
    return False, _collect_ids(result)


def _run_suite_parallel(jobs: int) -> tuple[bool, list[str]]:
    modules = _discover_modules()
    serial = [name for name in modules if name in _SERIAL_MODULES]
    parallel = [name for name in modules if name not in _SERIAL_MODULES]
    print(
        f"=== parallel unittest: jobs={jobs} "
        f"serial_lane={len(serial)} parallel_modules={len(parallel)} ==="
    )
    shards: list[_ShardResult] = []
    # 串行车道独占 1 个槽；其余给按模块拆开的纯单测。
    parallel_workers = max(1, jobs - 1)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as serial_pool:
        serial_future = serial_pool.submit(_spawn_shard, serial) if serial else None
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=parallel_workers
        ) as parallel_pool:
            parallel_futures = [
                parallel_pool.submit(_spawn_shard, [name]) for name in parallel
            ]
            for future in concurrent.futures.as_completed(parallel_futures):
                shards.append(future.result())
        if serial_future is not None:
            shards.append(serial_future.result())

    # 稳定打印汇总：串行车道最后已打印；这里给总数。
    failed_ids: list[str] = []
    tests_run = 0
    any_hard_fail = False
    for shard in shards:
        tests_run += shard.tests_run
        if shard.failed_ids:
            failed_ids.extend(shard.failed_ids)
        elif not shard.ok:
            # 加载期失败等拿不到 id：整轮判失败，不走偶发重跑。
            any_hard_fail = True
    print(
        f"=== parallel first pass: {tests_run} tests, "
        f"{len(failed_ids)} failed ids, hard_fail={any_hard_fail} ==="
    )
    if any_hard_fail and not failed_ids:
        return False, []
    if not failed_ids and not any_hard_fail:
        return True, []
    return False, failed_ids


def _retry_failed(failed_ids: list[str]) -> bool:
    if not failed_ids:
        return False
    print(f"\n=== 首轮 {len(failed_ids)} 个用例失败，按既定判定路径单独重跑一次 ===")
    for test_id in failed_ids:
        print(f"  - {test_id}")
    faulthandler.dump_traceback_later(HANG_DUMP_SECONDS, exit=True)
    _ensure_tests_on_path()
    loader = unittest.TestLoader()
    runner = unittest.TextTestRunner(verbosity=2)
    retry = runner.run(loader.loadTestsFromNames(failed_ids))
    if retry.wasSuccessful():
        print("\n=== 重跑全部通过，判定为已知偶发（非回归） ===")
        return True
    print("\n=== 重跑仍失败，判定为真回归 ===")
    return False


def _maybe_use_checkout_env() -> None:
    """完整套件使用已就绪的 checkout .venv 解释器。

    CI runner 没有 checkout .venv，原样走旧路径。本地存在 .venv 但未就绪时
    直接失败并给出 prepare 指引，不把完整套件跑在错误的依赖上（曾导致数十个
    用例因 SessKit 副本不一致而误报）。
    """
    if os.environ.get("CORRAL_CI_TEST_REEXEC") == "1":
        return
    venv_python = ROOT / ".venv" / "bin" / "python"
    if os.name == "nt":
        venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not venv_python.is_file():
        return
    try:
        if Path(sys.executable).resolve() == venv_python.resolve():
            return
    except OSError:
        return
    import dev_env  # noqa: E402 — scripts/ 目录已在 sys.path（见顶部）

    data, ready = dev_env.doctor(str(ROOT))
    if not ready:
        print("错误：checkout .venv 未就绪，拒绝在错误依赖下跑完整套件：", file=sys.stderr)
        for blocker in data.get("blockers", ()):
            print(f"  - {blocker}", file=sys.stderr)
        print(f"先跑：python3 scripts/dev_env.py prepare --repo {ROOT}", file=sys.stderr)
        raise SystemExit(1)
    os.environ["CORRAL_CI_TEST_REEXEC"] = "1"
    os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.run_modules is not None:
        os.environ.setdefault("CORRAL_ISOLATE_MANAGED_HOSTS", "1")
        return _worker_main(args.run_modules)

    if args.check_stamp:
        if ci_stamp.stamp_matches(ROOT):
            print("完整检查戳有效")
            return 0
        print("完整检查戳缺失或产品代码已改", file=sys.stderr)
        return 1

    if args.lint_only and args.skip_lint:
        print("错误：--lint-only 与 --skip-lint 不能同时使用", file=sys.stderr)
        return 2

    if not args.lint_only:
        _maybe_use_checkout_env()

    if not args.skip_lint:
        lint_code = _run_ruff()
        if lint_code != 0:
            return lint_code
        if args.lint_only:
            return 0
    elif args.lint_only:
        print("错误：--lint-only 与 --skip-lint 不能同时使用", file=sys.stderr)
        return 2

    # Keep developer keepalive panes out of SessionStore unit fixtures.
    os.environ.setdefault("CORRAL_ISOLATE_MANAGED_HOSTS", "1")

    jobs = args.jobs if args.jobs is not None else _default_jobs()
    if jobs < 1:
        print("错误：--jobs 必须 ≥ 1", file=sys.stderr)
        return 2

    wall0 = time.perf_counter()
    if jobs == 1:
        print("=== unittest: serial (jobs=1) ===")
        ok, failed_ids = _run_suite_serial()
    else:
        ok, failed_ids = _run_suite_parallel(jobs)
    print(f"=== first pass wall {time.perf_counter() - wall0:.1f}s ===")

    if ok:
        _record_success()
        return 0

    if not failed_ids:
        return 1

    if _retry_failed(failed_ids):
        _record_success()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
