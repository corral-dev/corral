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
# --list-ids 工序的回报行前缀；机器报告行前缀（父进程打印）。
_IDS_PREFIX = "CORRAL_CI_TEST_IDS:"
_REPORT_PREFIX = "CORRAL_CI_TEST_REPORT:"
# worker 输出里附带的用例 id 数量上限：全量约 2000 条 id（~100KB），父进程
# 消费后丢弃，不回显到日志。
_MAX_IDS_PER_SHARD = 10000
# 单个用例最慢 TopN（有界，不逐条记录全量耗时）。
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


class _TimingResult(unittest.TestResult):
    """带跳过计数、类级耗时、最慢用例 TopN 的结果收集器（仍是标准 unittest）。"""

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
        except Exception:  # noqa: BLE001 — 计时绝不能破坏测试结果
            return
        cls = test_id.rpartition(".")[0] or test_id
        self.class_seconds[cls] = self.class_seconds.get(cls, 0.0) + elapsed
        self.slowest.append((test_id, elapsed))
        if len(self.slowest) > _SLOWEST_TOP_N * 4:
            self.slowest = sorted(self.slowest, key=lambda kv: -kv[1])[: _SLOWEST_TOP_N]


def _iter_suite_ids(suite: unittest.TestSuite) -> list[str]:
    """不运行、只枚举 suite 里的全部用例 id（覆盖审计用）。"""
    ids: list[str] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            ids.extend(_iter_suite_ids(item))
        else:
            id_fn = getattr(item, "id", None)
            if callable(id_fn):
                try:
                    ids.append(id_fn())
                except Exception:  # noqa: BLE001 — 枚举失败由调用方判硬失败
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
        help=argparse.SUPPRESS,  # 内部：串行余量工序（模块名/module.Class 逗号分隔，无隔离）
    )
    parser.add_argument(
        "--run-shard",
        default=None,
        help=argparse.SUPPRESS,  # 内部：类粒度工序（module.Class 逗号分隔，需隔离）
    )
    parser.add_argument(
        "--run-tests",
        default=None,
        help=argparse.SUPPRESS,  # 内部：精确 id 重跑工序
    )
    parser.add_argument(
        "--list-ids",
        action="store_true",
        help=argparse.SUPPRESS,  # 内部：只枚举全量用例 id，不运行
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
    """在当前进程跑若干 test 模块 / 类 / 用例 id，供子进程工序使用。

    调用方必须已进入隔离（UI 工序在 load 之前进 ``isolated_test_resources``，
    因为 ``tests/test_ui.py`` 在 import 时就读 ``CORRAL_CACHE_DIR``）。
    """
    import io

    faulthandler.dump_traceback_later(HANG_DUMP_SECONDS, exit=True)
    _ensure_tests_on_path()
    loader = unittest.TestLoader()
    try:
        suite = loader.loadTestsFromNames(names)
    except Exception as exc:  # noqa: BLE001 — 加载失败也要结构化回报
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
    # 子进程输出由父进程转发；verbosity=2 与旧入口一致。
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
    """进入 B 的 tests/ci_test_support 隔离；缺失时返回 (None, False)。

    调用方必须在任何 test 模块 import/discovery 之前调用。
    """
    _ensure_tests_on_path()
    try:
        import ci_test_support  # type: ignore[import-not-found]  # noqa: E402
    except Exception as exc:  # noqa: BLE001 — 缺失或语法损坏都算不可用
        print(f"隔离 helper 不可用：{exc}", file=sys.stderr)
        return None, False
    try:
        manager = ci_test_support.isolated_test_resources()
        manager.__enter__()
    except AttributeError:
        return None, False
    return manager, True


def _worker_classes_main(spec_csv: str) -> int:
    """串行余量工序：类/模块粒度 specs，单进程顺序执行，无隔离。

    语义即今日串行车道（CORRAL_ISOLATE_MANAGED_HOSTS 由 main 分支 setdefault），
    专门承载未证明类：计时敏感的用例仍跑在串行语义下，且父进程将其与并行
    shards 分相执行，不受并行负载抖动影响。
    """
    names = [part.strip() for part in spec_csv.split(",") if part.strip()]
    if not names:
        return _worker_main("")
    shard = _run_names_in_process(names, isolated=False)
    return _emit_shard(shard)


def _worker_shard_main(spec_csv: str) -> int:
    """类粒度 UI 工序：必须有隔离 helper，否则明确拒绝（不用无隔离并行冒充）。"""
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
    """精确 id 重跑工序：有 helper 则进隔离（UI id 同样被隔离），否则沿用旧语义。"""
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
    """只枚举全量用例 id，不运行（覆盖审计的基准）。"""
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
    except Exception as exc:  # noqa: BLE001 — 枚举失败即硬失败
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
    # 用父进程墙钟覆盖（含子进程启动开销），便于对照。
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
    """读 B 的隔离证明名单；helper 缺失或名单为空时返回 None（走旧模块车道）。

    名单是 B 的证明结论，runner 只消费不推断：不在名单里的类一律进串行余量。
    """
    _ensure_tests_on_path()
    try:
        import ci_test_support  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 — helper 缺失或损坏时退回旧车道
        print(f"警告：tests/ci_test_support 不可用（{exc}），UI 类分片停用，走旧模块车道。")
        return None
    manifest = getattr(ci_test_support, "UI_SAFE_CLASSES", None)
    if not manifest:
        return None
    return frozenset(str(x) for x in manifest)


def _list_all_ids() -> dict[str, dict[str, list[str]]]:
    """起一次性子进程枚举全量 (module -> class -> ids)，不运行任何用例。"""
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


def _plan_shards(
    id_map: dict[str, dict[str, list[str]]],
    safe: frozenset[str] | None,
) -> tuple[list[list[str]], list[list[str]], list[str]]:
    """返回 (module_shards, class_shards, remainder_specs)。

    - 非串行模块：沿用既有语义，一个模块一 shard（--run-modules）。
    - 串行模块中、名单证明安全的类：类粒度 shard（--run-shard，需隔离）。
    - 串行模块的未证明类：逐类进入 remainder_specs（`module.Class`），由单个
      --run-classes 工序顺序执行（无隔离、即今日串行语义），保证恰一次。
      整个模块无一类被证明时，用整模块名占位（loadTestsFromNames 同语义）。
      名单里有、枚举里没有的条目（改名/删除残留）直接忽略，覆盖审计兜底。
    - helper 缺失/名单为空时：串行模块整体进 remainder（整模块名），
      行为与旧入口完全一致。
    """
    modules = sorted(id_map)
    parallel = [name for name in modules if name not in _SERIAL_MODULES]
    serial = [name for name in modules if name in _SERIAL_MODULES]
    module_shards = [[name] for name in parallel]
    class_shards: list[list[str]] = []
    remainder_specs: list[str] = []
    if safe:
        for name in serial:
            classes = sorted(id_map.get(name, {}))
            proven = sorted(c for c in classes if c in safe)
            unproven = [c for c in classes if c not in safe]
            class_shards.extend([[cls] for cls in proven])
            if unproven and not proven:
                remainder_specs.append(name)
            else:
                remainder_specs.extend(unproven)
    else:
        remainder_specs = serial
    return module_shards, class_shards, remainder_specs


def _audit_coverage(
    id_map: dict[str, dict[str, list[str]]], shards: list[_ShardResult]
) -> tuple[bool, list[str], list[str]]:
    """断言 shard 实际跑的 id 集合与枚举基准完全一致（不多不少、恰一次）。

    返回 (ok, missing, extra)。计数相等但集合不等同样判失败。
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


# 首轮报告上下文：main 在重跑后据此补发 final 行（修正 retried/retry_ok）。
_LAST_FIRST_PASS: dict = {}


def _run_suite_parallel(jobs: int) -> tuple[bool, list[str]]:
    wall0 = time.perf_counter()
    id_map = _list_all_ids()
    expected_total = sum(len(ids) for classes in id_map.values() for ids in classes.values())
    safe = _load_safe_classes()
    module_shards, class_shards, remainder_specs = _plan_shards(id_map, safe)
    serial_desc = (
        f"{len(remainder_specs)} specs" if safe else f"{len(remainder_specs)} modules"
    )
    parallel = [m for group in module_shards for m in group]
    print(
        f"=== parallel unittest: jobs={jobs} "
        f"serial_remainder=[{serial_desc}] parallel_modules={len(parallel)} "
        f"ui_class_shards={len(class_shards)} isolated={'yes' if safe else 'no'} "
        f"expected_tests={expected_total} ==="
    )
    shards: list[_ShardResult] = []
    # Phase 1：串行余量独占整机先行。未证明类（含计时敏感的 settle/wall-budget
    # 断言）跑在零并行负载下，与今日串行车道环境一致；余量仅约数十秒，
    # 先行失败还能早暴露加载期错误。
    if remainder_specs:
        shards.append(
            _spawn_worker("--run-classes", remainder_specs, kind="serial-remainder")
        )
    # Phase 2：其余模块 shard 与隔离类 shard 并行；两者互不共享资源。
    parallel_workers = max(1, jobs - 1)
    if module_shards or class_shards:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=parallel_workers
        ) as parallel_pool:
            parallel_futures = [
                parallel_pool.submit(_spawn_shard, group) for group in module_shards
            ]
            parallel_futures.extend(
                parallel_pool.submit(_spawn_worker, "--run-shard", cls, kind="ui-shard")
                for cls in class_shards
            )
            for future in concurrent.futures.as_completed(parallel_futures):
                shards.append(future.result())

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


def _retry_failed(failed_ids: list[str]) -> bool:
    if not failed_ids:
        return False
    print(f"\n=== 首轮 {len(failed_ids)} 个用例失败，按既定判定路径单独重跑一次 ===")
    for test_id in failed_ids:
        print(f"  - {test_id}")
    faulthandler.dump_traceback_later(HANG_DUMP_SECONDS, exit=True)
    # 重跑走隔离 worker（有 helper 时 UI id 同样被隔离；无 helper 时语义与旧
    # 单进程重跑一致，只是由子进程承载）。
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
    """同一工作区源码指纹 + 环境指纹；stamp 前后对比，树在中途变了就不写戳。"""
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
    # 补发 final 行：first-pass 行的 retried 恒为 0（重跑尚未发生），final 行
    # 纠正为实际执行的重跑数。phase 键为新增；其余 schema 不变。
    if _LAST_FIRST_PASS:
        ctx = _LAST_FIRST_PASS
        _emit_machine_report(
            phase="final",
            wall=ctx["wall"],
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
