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
# --list-ids worker reply prefix; machine report prefix (parent prints).
_IDS_PREFIX = "CORRAL_CI_TEST_IDS:"
_REPORT_PREFIX = "CORRAL_CI_TEST_REPORT:"
# Cap on test ids attached to worker output: ~2000 ids (~100KB) for a full
# suite; the parent consumes and drops them, never echoing into logs.
_MAX_IDS_PER_SHARD = 10000
# Slowest-TopN per test (bounded; never records every test duration).
_SLOWEST_TOP_N = 10

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
    skipped: int = 0
    isolated: bool = False
    class_seconds: tuple[tuple[str, float], ...] = ()
    slowest: tuple[tuple[str, float], ...] = ()
    test_ids: tuple[str, ...] = ()


class _TimingResult(unittest.TextTestResult):
    """Result collector with skip counts, per-class seconds, slowest-TopN.

    Must subclass ``unittest.TextTestResult`` (the runner's text result class),
    never plain ``unittest.TestResult``: the latter's ``printErrors`` is an
    empty no-op, and all ``FAIL:``/``ERROR:`` progress lines plus traceback
    printing live on the TextTestResult side. The wrong base leaves failures
    with only ``FAILED (failures=1)`` and zero diagnostics (seen on cloud
    2026-09-30).
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.skipped_count = 0
        self.class_seconds: dict[str, float] = {}
        self.slowest: list[tuple[str, float]] = []
        self._test_started = 0.0

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:  # noqa: N802
        super().addSkip(test, reason)
        self.skipped_count += 1

    def startTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().startTest(test)
        self._test_started = time.perf_counter()

    def stopTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        elapsed = time.perf_counter() - self._test_started
        super().stopTest(test)
        try:
            test_id = test.id()
        except Exception:  # noqa: BLE001 - timing must never break results
            return
        cls = test_id.rpartition(".")[0] or test_id
        self.class_seconds[cls] = self.class_seconds.get(cls, 0.0) + elapsed
        self.slowest.append((test_id, elapsed))
        if len(self.slowest) > _SLOWEST_TOP_N * 4:
            self.slowest = sorted(self.slowest, key=lambda kv: -kv[1])[: _SLOWEST_TOP_N]


def _iter_suite_ids(suite: unittest.TestSuite) -> list[str]:
    """Enumerate every test id in a suite without running (coverage audit)."""
    ids: list[str] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            ids.extend(_iter_suite_ids(item))
        else:
            id_fn = getattr(item, "id", None)
            if callable(id_fn):
                try:
                    ids.append(id_fn())
                except Exception:  # noqa: BLE001 - caller treats enum failure as hard fail
                    ids.append(f"<unlistable:{type(item).__name__}>")
    return ids


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
    parser.add_argument(
        "--run-classes",
        default=None,
        help=argparse.SUPPRESS,  # internal: serial-remainder worker (module or module.Class CSV, unisolated)
    )
    parser.add_argument(
        "--run-shard",
        default=None,
        help=argparse.SUPPRESS,  # internal: class-granularity worker (module.Class CSV, needs isolation)
    )
    parser.add_argument(
        "--run-tests",
        default=None,
        help=argparse.SUPPRESS,  # internal: exact-id retry worker
    )
    parser.add_argument(
        "--list-ids",
        action="store_true",
        help=argparse.SUPPRESS,  # internal: enumerate all test ids only, no run
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


def _run_names_in_process(
    names: list[str], *, isolated: bool = False
) -> _ShardResult:
    """Run test modules / classes / test ids in this process for workers.

    The caller must already hold isolation (UI workers enter
    ``isolated_test_resources`` before load, because ``tests/test_ui.py``
    reads ``CORRAL_CACHE_DIR`` at import time).
    """
    import io

    faulthandler.dump_traceback_later(HANG_DUMP_SECONDS, exit=True)
    _ensure_tests_on_path()
    loader = unittest.TestLoader()
    try:
        suite = loader.loadTestsFromNames(names)
    except Exception as exc:  # noqa: BLE001 - load failure still needs a structured report
        import traceback

        return _ShardResult(
            modules=tuple(names),
            ok=False,
            failed_ids=(),
            tests_run=0,
            seconds=0.0,
            output=f"load failure for {names}: {exc}\n{traceback.format_exc()}",
            returncode=1,
            isolated=isolated,
        )
    test_ids = tuple(_iter_suite_ids(suite))
    # Worker output is forwarded by the parent; verbosity=2 matches the old entry.
    buf = io.StringIO()
    runner = unittest.TextTestRunner(stream=buf, verbosity=2, resultclass=_TimingResult)
    started = time.perf_counter()
    result = runner.run(suite)
    seconds = time.perf_counter() - started
    failed = tuple(_collect_ids(result))
    slowest = (
        tuple(sorted(result.slowest, key=lambda kv: -kv[1])[:_SLOWEST_TOP_N])
        if isinstance(result, _TimingResult)
        else ()
    )
    return _ShardResult(
        modules=tuple(names),
        ok=result.wasSuccessful(),
        failed_ids=failed,
        tests_run=result.testsRun,
        seconds=seconds,
        output=buf.getvalue(),
        returncode=0 if result.wasSuccessful() else 1,
        skipped=result.skipped_count if isinstance(result, _TimingResult) else 0,
        isolated=isolated,
        class_seconds=tuple(
            sorted(result.class_seconds.items()) if isinstance(result, _TimingResult) else ()
        ),
        slowest=slowest,
        test_ids=test_ids[:_MAX_IDS_PER_SHARD],
    )


def _run_modules_in_process(module_names: list[str]) -> _ShardResult:
    """在当前进程跑若干 test_* 模块，供 --run-modules 子进程使用。"""
    return _run_names_in_process(module_names)


def _emit_shard(shard: _ShardResult) -> int:
    sys.stdout.write(shard.output)
    if shard.output and not shard.output.endswith("\n"):
        sys.stdout.write("\n")
    payload = {
        "ok": shard.ok,
        "failed_ids": list(shard.failed_ids),
        "tests_run": shard.tests_run,
        "seconds": round(shard.seconds, 3),
        "modules": list(shard.modules),
        "skipped": shard.skipped,
        "isolated": shard.isolated,
        "class_seconds": {k: round(v, 3) for k, v in shard.class_seconds},
        "slowest": [(tid, round(sec, 3)) for tid, sec in shard.slowest],
        "test_ids": list(shard.test_ids),
    }
    print(f"{_RESULT_PREFIX}{json.dumps(payload, ensure_ascii=False)}")
    return shard.returncode


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
    return _emit_shard(shard)


def _enter_shard_isolation() -> tuple[object | None, bool]:
    """Enter B's tests/ci_test_support isolation; (None, False) when absent.

    Callers must invoke this before any test-module import/discovery.
    """
    _ensure_tests_on_path()
    try:
        import ci_test_support  # type: ignore[import-not-found]  # noqa: E402
    except Exception as exc:  # noqa: BLE001 - missing or syntactically broken both count as unavailable
        print(f"隔离 helper 不可用：{exc}", file=sys.stderr)
        return None, False
    try:
        manager = ci_test_support.isolated_test_resources()
        manager.__enter__()
    except AttributeError:
        return None, False
    return manager, True


def _worker_classes_main(spec_csv: str) -> int:
    """Serial-remainder worker: class/module specs, sequential, unisolated.

    Same semantics as today's serial lane (CORRAL_ISOLATE_MANAGED_HOSTS via the
    main-branch setdefault). Carries unproven classes: timing-sensitive tests
    still run under serial semantics, and the parent phases them apart from
    parallel shards so parallel load skew cannot reach them.
    """
    names = [part.strip() for part in spec_csv.split(",") if part.strip()]
    if not names:
        return _worker_main("")
    shard = _run_names_in_process(names, isolated=False)
    return _emit_shard(shard)


def _worker_shard_main(spec_csv: str) -> int:
    """Class-granularity UI worker: requires the isolation helper, else refuse
    outright (never fake it with unisolated parallelism)."""
    names = [part.strip() for part in spec_csv.split(",") if part.strip()]
    if not names:
        return _worker_main("")
    manager, isolated = _enter_shard_isolation()
    if not isolated:
        print(
            "错误：类粒度工序需要 tests/ci_test_support.isolated_test_resources，"
            "当前工作区缺失，拒绝无隔离并行。",
            file=sys.stderr,
        )
        return 2
    os.environ["CORRAL_CI_SHARD"] = "1"
    try:
        shard = _run_names_in_process(names, isolated=True)
    finally:
        try:
            manager.__exit__(None, None, None)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 — teardown 失败必须可见
            print(f"警告：隔离 teardown 失败：{exc}", file=sys.stderr)
    return _emit_shard(shard)


def _worker_tests_main(spec_csv: str) -> int:
    """Exact-id retry worker: enters isolation when the helper exists (UI ids
    isolated too), otherwise keeps the old semantics."""
    names = [part.strip() for part in spec_csv.split(",") if part.strip()]
    if not names:
        return _worker_main("")
    manager, isolated = _enter_shard_isolation()
    if isolated:
        os.environ["CORRAL_CI_SHARD"] = "1"
    try:
        shard = _run_names_in_process(names, isolated=isolated)
    finally:
        if manager is not None:
            try:
                manager.__exit__(None, None, None)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                print(f"警告：隔离 teardown 失败：{exc}", file=sys.stderr)
    return _emit_shard(shard)


def _worker_list_ids_main() -> int:
    """Enumerate all test ids without running (coverage-audit baseline)."""
    faulthandler.dump_traceback_later(HANG_DUMP_SECONDS, exit=True)
    _ensure_tests_on_path()
    loader = unittest.TestLoader()
    modules = _discover_modules()
    out: dict[str, dict[str, list[str]]] = {}
    try:
        for module in modules:
            suite = loader.loadTestsFromName(module)
            classes: dict[str, list[str]] = {}
            for test_id in _iter_suite_ids(suite):
                cls = test_id.rpartition(".")[0] or test_id
                classes.setdefault(cls, []).append(test_id)
            out[module] = classes
    except Exception as exc:  # noqa: BLE001 - enumeration failure is a hard fail
        import traceback

        sys.stdout.write(traceback.format_exc())
        print(f"{_IDS_PREFIX}{json.dumps({'ok': False, 'error': str(exc)})}")
        return 1
    print(f"{_IDS_PREFIX}{json.dumps({'ok': True, 'modules': out})}")
    return 0


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
    class_seconds = payload.get("class_seconds") or {}
    slowest = payload.get("slowest") or []
    test_ids = payload.get("test_ids") or []
    return _ShardResult(
        modules=tuple(payload.get("modules") or modules),
        ok=bool(payload.get("ok")) and returncode == 0 and not failed,
        failed_ids=failed,
        tests_run=int(payload.get("tests_run") or 0),
        seconds=float(payload.get("seconds") or 0.0),
        output=output,
        returncode=returncode,
        skipped=int(payload.get("skipped") or 0),
        isolated=bool(payload.get("isolated")),
        class_seconds=tuple(
            (str(k), float(v)) for k, v in sorted(class_seconds.items())
        ),
        slowest=tuple((str(t), float(s)) for t, s in slowest[:_SLOWEST_TOP_N]),
        test_ids=tuple(str(t) for t in test_ids[:_MAX_IDS_PER_SHARD]),
    )


def _spawn_worker(mode_flag: str, spec: list[str], *, kind: str = "shard") -> _ShardResult:
    if not spec:
        return _ShardResult((), True, (), 0, 0.0, "", 0)
    label = ",".join(spec)
    print(f"=== {kind} start ({len(spec)} item{'s' if len(spec) != 1 else ''}): {label} ===")
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), mode_flag, label],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    blob = (proc.stdout or "") + (proc.stderr or "")
    shard = _parse_worker_output(blob, spec, proc.returncode)
    # Overwrite with the parent wall clock (includes spawn overhead) for comparison.
    shard = _ShardResult(
        modules=shard.modules,
        ok=shard.ok,
        failed_ids=shard.failed_ids,
        tests_run=shard.tests_run,
        seconds=time.perf_counter() - started,
        output=shard.output,
        returncode=shard.returncode,
        skipped=shard.skipped,
        isolated=shard.isolated,
        class_seconds=shard.class_seconds,
        slowest=shard.slowest,
        test_ids=shard.test_ids,
    )
    sys.stdout.write(shard.output)
    status = "ok" if shard.ok else "FAIL"
    print(
        f"=== {kind} {status} in {shard.seconds:.1f}s "
        f"({shard.tests_run} tests, {shard.skipped} skipped): {label} ==="
    )
    return shard


def _spawn_shard(modules: list[str]) -> _ShardResult:
    return _spawn_worker("--run-modules", modules)


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


def _load_safe_classes() -> frozenset[str] | None:
    """Read B's isolation-proof manifest; None when the helper is missing or the
    manifest is empty (legacy module lane).

    The manifest is B's proven conclusion; the runner consumes it without
    inferring: any class absent from it goes to the serial remainder.
    """
    _ensure_tests_on_path()
    try:
        import ci_test_support  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 - fall back to the legacy lane when the helper is missing or broken
        print(f"警告：tests/ci_test_support 不可用（{exc}），UI 类分片停用，走旧模块车道。")
        return None
    manifest = getattr(ci_test_support, "UI_SAFE_CLASSES", None)
    if not manifest:
        return None
    return frozenset(str(x) for x in manifest)


def _list_all_ids() -> dict[str, dict[str, list[str]]]:
    """Spawn a one-shot subprocess enumerating all (module -> class -> ids); runs nothing."""
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--list-ids"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    blob = (proc.stdout or "") + (proc.stderr or "")
    for line in blob.splitlines():
        if line.startswith(_IDS_PREFIX):
            try:
                payload = json.loads(line[len(_IDS_PREFIX):])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("ok"):
                modules = payload.get("modules")
                if isinstance(modules, dict):
                    return modules
    raise RuntimeError(f"--list-ids 枚举失败 (exit={proc.returncode})：{blob[-2000:]}")


# Ordinary-module batching: tiny modules share one worker to cut cold starts.
# Sequential co-location is a subset of legacy --jobs=1 semantics, so no
# isolation proof is needed for these (B/C retain veto).
MODULE_BATCH_SIZE = 8
# Grouped proven-UI workers: caps per worker (B proved 21 classes / 96 cases
# together; subsets of a jointly-proven set stay safe by running sequentially).
GROUP_MAX_CLASSES = 25
GROUP_MAX_TESTS = 120


def _load_batch_manifest() -> tuple[frozenset[str] | None, dict[str, int] | None]:
    """Read B's grouping/splitting manifests from the helper (validated).

    Returns (groupable, splittable); either is None when absent or malformed.
    Grouping/splitting additionally require UI_SAFE_CLASSES membership, checked
    by the planner, so a stray entry can never widen proven scope by itself.
    """
    _ensure_tests_on_path()
    try:
        import ci_test_support  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 - missing or broken counts as absent
        print(f"Batch manifest unavailable: {exc}")
        return None, None
    groupable = getattr(ci_test_support, "UI_GROUPABLE_CLASSES", None)
    splittable = getattr(ci_test_support, "UI_SPLITTABLE_CLASSES", None)
    if groupable is not None and not (
        isinstance(groupable, (frozenset, set))
        and all(isinstance(x, str) for x in groupable)
    ):
        print("Warning: UI_GROUPABLE_CLASSES malformed, ignoring.")
        groupable = None
    if splittable is not None and not (
        isinstance(splittable, dict)
        and all(isinstance(k, str) and isinstance(v, int) and v >= 2 for k, v in splittable.items())
    ):
        print("Warning: UI_SPLITTABLE_CLASSES malformed, ignoring.")
        splittable = None
    return (
        frozenset(groupable) if groupable is not None else None,
        dict(splittable) if splittable is not None else None,
    )


def _chunk_ids(ids: list[str], n: int) -> list[list[str]]:
    """Split sorted ids into at most n contiguous deterministic chunks."""
    if n < 2 or len(ids) <= 1:
        return [list(ids)]
    n = min(n, len(ids))
    base, rem = divmod(len(ids), n)
    chunks: list[list[str]] = []
    start = 0
    for i in range(n):
        size = base + (1 if i < rem else 0)
        chunks.append(ids[start : start + size])
        start += size
    return [c for c in chunks if c]


def _plan_shards(
    id_map: dict[str, dict[str, list[str]]],
    safe: frozenset[str] | None,
    *,
    groupable: frozenset[str] | None = None,
    splittable: dict[str, int] | None = None,
) -> tuple[list[list[str]], list[list[str]], list[str]]:
    """Return (module_batches, ui_workers, remainder_specs).

    - Non-serial modules: deterministic contiguous batches of at most
      MODULE_BATCH_SIZE (--run-modules each). Same env and sequential
      in-process semantics as the legacy lane.
    - Serial-lane classes proven by the manifest: --run-shard workers, where a
      worker spec is one class, one B-allowlisted group of small classes, or
      one method-ID chunk of a B-allowlisted heavyweight class. Every worker
      still enters isolation once and runs sequentially.
    - Unproven classes: remainder_specs as before (quiet serial phase).
    - Stale manifest entries (absent from the live enumeration, or not in the
      safe set) are ignored; the coverage audit backstops drift.
    - Helper missing/manifests empty: one module per batch, one class per
      worker, whole serial modules in the remainder — identical to before.
    """
    modules = sorted(id_map)
    parallel = [name for name in modules if name not in _SERIAL_MODULES]
    serial = [name for name in modules if name in _SERIAL_MODULES]
    module_batches = [parallel[i : i + MODULE_BATCH_SIZE] for i in range(0, len(parallel), MODULE_BATCH_SIZE)]
    groupable_eff = (groupable or frozenset()) & (safe or frozenset())
    splittable_eff = {
        cls: n
        for cls, n in (splittable or {}).items()
        if cls in (safe or frozenset()) and n >= 2
    }
    ui_workers: list[list[str]] = []
    group_classes: list[str] = []

    remainder_specs: list[str] = []
    if safe:
        for name in serial:
            classes = sorted(id_map.get(name, {}))
            proven = sorted(c for c in classes if c in safe)
            unproven = [c for c in classes if c not in safe]
            for cls in proven:
                ids = sorted(id_map[name][cls])
                if cls in splittable_eff and len(ids) > 1:
                    for chunk in _chunk_ids(ids, splittable_eff[cls]):
                        ui_workers.append(chunk)
                elif cls in groupable_eff:
                    # Collected separately and batched after the loop: proven
                    # singles interleaved in name order must not fragment the
                    # group into many tiny workers.
                    group_classes.append(cls)
                else:
                    ui_workers.append([cls])
            if unproven and not proven:
                remainder_specs.append(name)
            else:
                remainder_specs.extend(unproven)
    else:
        remainder_specs = serial
    for block, _ in _group_blocks(group_classes, id_map):
        ui_workers.append(block)
    return module_batches, ui_workers, remainder_specs


def _group_blocks(
    group_classes: list[str], id_map: dict[str, dict[str, list[str]]]
) -> list[tuple[list[str], int]]:
    """Pack allowlisted group classes into workers within both caps.

    Returns (block, test_count) pairs, preserving manifest order.
    """
    blocks: list[tuple[list[str], int]] = []
    block: list[str] = []
    tests = 0
    for cls in group_classes:
        n = len(id_map.get(cls.split(".")[0], {}).get(cls, []))
        if block and (len(block) + 1 > GROUP_MAX_CLASSES or tests + n > GROUP_MAX_TESTS):
            blocks.append((block, tests))
            block = []
            tests = 0
        block.append(cls)
        tests += n
    if block:
        blocks.append((block, tests))
    return blocks


def _order_phase2(
    module_batches: list[list[str]],
    ui_workers: list[list[str]],
    id_map: dict[str, dict[str, list[str]]],
) -> list[tuple[str, list[str]]]:
    """Order phase-2 submissions heavier-first for load balance.

    Weight is the live test count within each tier (deterministic; correlates
    with UI duration), tie-broken by spec name. UI workers always submit before
    module batches: a whole ordinary module costs at most seconds (measured
    max ~2 s), while one UI class can cost a minute, so cross-kind
    count comparison would misfire. Returns (mode_flag, spec) pairs.
    """
    def module_count(name: str) -> int:
        return sum(len(ids) for ids in id_map.get(name, {}).values())

    def worker_count(spec: list[str]) -> int:
        total = 0
        for item in spec:
            parts = item.split(".")
            if len(parts) == 1:
                total += module_count(item)
            elif len(parts) == 2:
                total += len(id_map.get(parts[0], {}).get(item, []))
            else:
                total += 1  # a method-ID spec runs exactly one test
        return total

    ordered: list[tuple[int, int, str, str, list[str]]] = []
    for spec in ui_workers:
        ordered.append((0, -worker_count(spec), spec[0], "--run-shard", spec))
    for batch in module_batches:
        ordered.append(
            (1, -sum(module_count(m) for m in batch), ",".join(batch), "--run-modules", batch)
        )
    ordered.sort()
    return [(flag, spec) for _, _, _, flag, spec in ordered]


def _audit_coverage(
    id_map: dict[str, dict[str, list[str]]], shards: list[_ShardResult]
) -> tuple[bool, list[str], list[str]]:
    """Assert the union of shard-run ids equals the enumeration baseline exactly
    (no more, no less, exactly once).

    Returns (ok, missing, extra). Equal counts with unequal sets still fail.
    """
    expected: list[str] = []
    for module in sorted(id_map):
        for cls in sorted(id_map[module]):
            expected.extend(id_map[module][cls])
    ran: list[str] = []
    for shard in shards:
        ran.extend(shard.test_ids)
    expected_counts: dict[str, int] = {}
    for test_id in expected:
        expected_counts[test_id] = expected_counts.get(test_id, 0) + 1
    ran_counts: dict[str, int] = {}
    for test_id in ran:
        ran_counts[test_id] = ran_counts.get(test_id, 0) + 1
    missing = sorted(t for t, n in expected_counts.items() if ran_counts.get(t, 0) < n)
    extra = sorted(t for t, n in ran_counts.items() if expected_counts.get(t, 0) < n)
    return (not missing and not extra and len(ran) == len(expected)), missing, extra


def _emit_machine_report(
    *,
    phase: str = "first-pass",
    wall: float,
    jobs: int,
    shards: list[_ShardResult],
    expected_total: int,
    retried: int,
    retry_ok: bool,
    coverage_ok: bool,
    missing: list[str],
    extra: list[str],
) -> dict:
    slowest: list[tuple[str, float]] = []
    class_seconds: dict[str, float] = {}
    for shard in shards:
        slowest.extend(shard.slowest)
        for cls, sec in shard.class_seconds:
            class_seconds[cls] = class_seconds.get(cls, 0.0) + sec
    slowest = sorted(slowest, key=lambda kv: -kv[1])[:_SLOWEST_TOP_N]
    report = {
        "phase": phase,
        "wall_seconds": round(wall, 1),
        "jobs": jobs,
        "expected_tests": expected_total,
        "ran_tests": sum(s.tests_run for s in shards),
        "skipped": sum(s.skipped for s in shards),
        "failed_first_pass": sum(len(s.failed_ids) for s in shards),
        "retried": retried,
        "retry_ok": retry_ok,
        "coverage_ok": coverage_ok,
        "missing_ids": missing[:50],
        "extra_ids": extra[:50],
        "shards": [
            {
                "labels": list(s.modules),
                "tests": s.tests_run,
                "skipped": s.skipped,
                "seconds": round(s.seconds, 1),
                "isolated": s.isolated,
                "ok": s.ok,
            }
            for s in sorted(shards, key=lambda s: -s.seconds)
        ],
        "slowest_classes": [
            (cls, round(sec, 1))
            for cls, sec in sorted(class_seconds.items(), key=lambda kv: -kv[1])[:20]
        ],
        "slowest_tests": [(tid, round(sec, 2)) for tid, sec in slowest],
    }
    print(f"{_REPORT_PREFIX}{json.dumps(report, ensure_ascii=False)}")
    return report


# First-pass report context: main re-emits the final line from this after retry
# (correcting retried/retry_ok).
_LAST_FIRST_PASS: dict = {}


def _kind_for(flag: str, spec: list[str]) -> str:
    """Short worker label for logs: chunk (method IDs), group, or class."""
    if flag != "--run-shard" or not spec:
        return "shard"
    if len(spec) == 1:
        return "ui-chunk" if spec[0].count(".") >= 2 else "ui-shard"
    return "ui-group"


def _run_suite_parallel(jobs: int) -> tuple[bool, list[str]]:
    wall0 = time.perf_counter()
    id_map = _list_all_ids()
    expected_total = sum(len(ids) for classes in id_map.values() for ids in classes.values())
    safe = _load_safe_classes()
    groupable, splittable = _load_batch_manifest()
    module_batches, ui_workers, remainder_specs = _plan_shards(
        id_map, safe, groupable=groupable, splittable=splittable
    )
    serial_desc = (
        f"{len(remainder_specs)} specs" if safe else f"{len(remainder_specs)} modules"
    )
    parallel = [m for group in module_batches for m in group]
    print(
        f"=== parallel unittest: jobs={jobs} "
        f"serial_remainder=[{serial_desc}] parallel_modules={len(parallel)} "
        f"ui_workers={len(ui_workers)} isolated={'yes' if safe else 'no'} "
        f"expected_tests={expected_total} ==="
    )
    shards: list[_ShardResult] = []
    # Phase 1: the serial remainder runs first, alone on the machine. Unproven
    # classes (including timing-sensitive settle/wall-budget asserts) run under
    # zero parallel load, matching today's serial-lane environment; the remainder
    # costs only tens of seconds, and an early failure exposes load errors fast.
    if remainder_specs:
        shards.append(
            _spawn_worker("--run-classes", remainder_specs, kind="serial-remainder")
        )
    # Phase 2: module batches and isolated UI workers run heavier-first in
    # parallel; the groups share no resources. The full jobs budget applies
    # here (see parallel_workers): Phase 1 already finished, so nothing needs
    # reserving.
    parallel_workers = max(1, jobs)  # Phase 1 already finished; no slot reserve.
    submissions = _order_phase2(module_batches, ui_workers, id_map)
    if submissions:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=parallel_workers
        ) as parallel_pool:
            parallel_futures = [
                parallel_pool.submit(_spawn_shard, spec)
                if flag == "--run-modules"
                else parallel_pool.submit(
                    _spawn_worker, flag, spec, kind=_kind_for(flag, spec)
                )
                for flag, spec in submissions
            ]
            for future in concurrent.futures.as_completed(parallel_futures):
                shards.append(future.result())

    # Stable summary print: the serial lane already printed last; totals here.
    failed_ids: list[str] = []
    tests_run = 0
    any_hard_fail = False
    for shard in shards:
        tests_run += shard.tests_run
        if shard.failed_ids:
            failed_ids.extend(shard.failed_ids)
        elif not shard.ok:
            # Load-time failures with no ids: fail the round, no flaky-retry path.
            any_hard_fail = True
    coverage_ok, missing, extra = _audit_coverage(id_map, shards)
    if not coverage_ok:
        print(
            f"=== COVERAGE AUDIT FAIL: expected={expected_total} ran={tests_run} "
            f"missing={len(missing)} extra={len(extra)} ==="
        )
        for test_id in missing[:20]:
            print(f"  missing: {test_id}")
        for test_id in extra[:20]:
            print(f"  extra: {test_id}")
    print(
        f"=== parallel first pass: {tests_run} tests, "
        f"{len(failed_ids)} failed ids, hard_fail={any_hard_fail} "
        f"coverage_ok={coverage_ok} ==="
    )
    _LAST_FIRST_PASS.update(
        {
            "suite_start": wall0,
            "wall": time.perf_counter() - wall0,
            "jobs": jobs,
            "shards": shards,
            "expected_total": expected_total,
            "coverage_ok": coverage_ok,
            "missing": missing,
            "extra": extra,
        }
    )
    _emit_machine_report(
        phase="first-pass",
        wall=time.perf_counter() - wall0,
        jobs=jobs,
        shards=shards,
        expected_total=expected_total,
        retried=0,
        retry_ok=False,
        coverage_ok=coverage_ok,
        missing=missing,
        extra=extra,
    )
    if not coverage_ok:
        return False, []
    if any_hard_fail and not failed_ids:
        return False, []
    if not failed_ids and not any_hard_fail:
        return True, []
    return False, failed_ids


def _final_wall_seconds(ctx: dict) -> float:
    """Final-line wall clock: suite start (all first-pass phases) through the
    end of retry.

    Outer lint stays separately metered and excluded; retry elapsed is included.
    """
    return time.perf_counter() - ctx["suite_start"]


def _retry_failed(failed_ids: list[str]) -> bool:
    if not failed_ids:
        return False
    print(f"\n=== 首轮 {len(failed_ids)} 个用例失败，按既定判定路径单独重跑一次 ===")
    for test_id in failed_ids:
        print(f"  - {test_id}")
    faulthandler.dump_traceback_later(HANG_DUMP_SECONDS, exit=True)
    # Retry runs through the isolated worker (UI ids isolated too when the
    # helper exists; without it the semantics match the old single-process
    # retry, only carried by a subprocess).
    shard = _spawn_worker("--run-tests", sorted(set(failed_ids)), kind="retry")
    if shard.ok and not shard.failed_ids:
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


def _fingerprints() -> tuple[str, str]:
    """Same-workspace source + environment fingerprints; compared before/after
    the tests so a mid-run tree change refuses the stamp."""
    return ci_stamp.worktree_fingerprint(ROOT), ci_stamp.environment_fingerprint(ROOT)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.list_ids:
        os.environ.setdefault("CORRAL_ISOLATE_MANAGED_HOSTS", "1")
        return _worker_list_ids_main()
    if args.run_classes is not None:
        os.environ.setdefault("CORRAL_ISOLATE_MANAGED_HOSTS", "1")
        return _worker_classes_main(args.run_classes)
    if args.run_shard is not None:
        os.environ.setdefault("CORRAL_ISOLATE_MANAGED_HOSTS", "1")
        return _worker_shard_main(args.run_shard)
    if args.run_tests is not None:
        os.environ.setdefault("CORRAL_ISOLATE_MANAGED_HOSTS", "1")
        return _worker_tests_main(args.run_tests)
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
    fp_before = _fingerprints()
    if jobs == 1:
        print("=== unittest: serial (jobs=1) ===")
        ok, failed_ids = _run_suite_serial()
    else:
        ok, failed_ids = _run_suite_parallel(jobs)
    print(f"=== first pass wall {time.perf_counter() - wall0:.1f}s ===")

    if ok:
        fp_after = _fingerprints()
        if fp_after != fp_before:
            print(
                "=== 拒绝写戳：测试前后源码/环境指纹变化（树在中途被改动），"
                "本轮结果不能复用为完整检查戳 ===",
                file=sys.stderr,
            )
            print(f"  before={fp_before}")
            print(f"  after ={fp_after}")
            return 0
        _record_success()
        return 0

    if not failed_ids:
        return 1

    retried = len(set(failed_ids))
    retry_ok = _retry_failed(failed_ids)
    # Re-emit the final line: the first-pass line always carries retried=0
    # (retry has not happened yet); the final line corrects it to the retry
    # count actually executed. The phase key is the only addition; the rest
    # of the schema is unchanged.
    if _LAST_FIRST_PASS:
        ctx = _LAST_FIRST_PASS
        _emit_machine_report(
            phase="final",
            wall=_final_wall_seconds(ctx),
            jobs=ctx["jobs"],
            shards=ctx["shards"],
            expected_total=ctx["expected_total"],
            retried=retried,
            retry_ok=retry_ok,
            coverage_ok=ctx["coverage_ok"],
            missing=ctx["missing"],
            extra=ctx["extra"],
        )
    if retry_ok:
        fp_after = _fingerprints()
        if fp_after != fp_before:
            print(
                "=== 拒绝写戳：重跑前后源码/环境指纹变化，本轮结果不能复用 ===",
                file=sys.stderr,
            )
            return 0
        _record_success()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
