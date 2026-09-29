"""E6 — one persistent coordinator session over a long scripted timeline.

Usage: python long_run.py [--restart-at N] [--limit K]
Round 1 sends the full prompt; later rounds send only the round payload to the
same session (created with --session-id <uuid>, continued with --resume; plain
-p --no-session-persistence cannot be resumed). A deterministic ledger
simulator applies the coordinator's validated actions (create_task queued -> running when
dispatched, stop, mark_done, reassign, reopen_as_followup; ask inserts an
<!--coordinator--> block near the quoted block); rejections from contract.validate go
back to the coordinator once per round, like run_eval.run_one. --restart-at N starts a
fresh session at round N with the full prompt plus the current ledger/document
only. Writes results/e6-<mode>.json and prints a summary (pass count, token
growth per round, latency trend).
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from contract import validate
from run_eval import PROMPT, parse, plain, quotes_exact
from scenarios import ASSISTANTS, PROJECTS, kinds

OPEN, CLOSE = "<!--coordinator-->", "<!--/coordinator-->"  # marks text the coordinator wrote

HERE = Path(__file__).resolve().parent

NOT_JSON = ('\n\n## Your previous answer was not a JSON object\n\n'
            'Return only the JSON object {"actions": [...], "note": "..."}.')
REJECTED = ("\n\n## Your previous answer was rejected by the command boundary\n\n"
            "Your actions:\n```json\n{actions}\n```\nReasons:\n- {reasons}\n\n"
            "Return a corrected, complete JSON object.")


def round_payload(document: list[str], changes: list[dict], ledger: list[dict],
                  assistants: dict) -> str:
    doc = [{"block": i + 1, "text": t} for i, t in enumerate(document)]
    return json.dumps({"document": doc, "changes": changes, "ledger": ledger,
                       "projects": PROJECTS, "assistants": assistants},
                      ensure_ascii=False, indent=1)


def find_block(document: list[str], needle: str) -> int:
    for i, block in enumerate(document):
        if needle in plain(block):
            return i
    raise ValueError(f"timeline op matches no block: {needle!r}")


def apply_owner(document: list[str], ops: list[tuple]) -> tuple[list[str], list[dict]]:
    """Scripted owner edits. Blocks are matched by substring of their plain text."""
    doc, changes = list(document), []
    for op in ops:
        kind = op[0]
        if kind == "add":
            doc.append(op[1])
            changes.append({"block": len(doc), "kind": "added"})
        elif kind == "add_after_question":  # answer just below the coordinator's question on the matched block
            i = find_block(doc, op[1]) + 1
            if i < len(doc) and doc[i].startswith("<!--coordinator-->"):
                i += 1
            doc.insert(i, op[2])
            changes.append({"block": i + 1, "kind": "added"})
        elif kind == "edit":
            i = find_block(doc, op[1])
            changes.append({"block": i + 1, "kind": "edited", "before": doc[i], "after": op[2]})
            doc[i] = op[2]
        elif kind == "strike":  # withdraw: the whole block is struck, characters kept
            i = find_block(doc, op[1])
            changes.append({"block": i + 1, "kind": "struck", "before": doc[i],
                            "after": f"~~{doc[i]}~~"})
            doc[i] = f"~~{doc[i]}~~"
        elif kind == "strike_part":  # typo fix: strike the old word, write the new one
            i = find_block(doc, op[1])
            after = doc[i].replace(op[2], f"~~{op[2]}~~{op[3]}", 1)
            changes.append({"block": i + 1, "kind": "edited", "before": doc[i], "after": after})
            doc[i] = after
        else:
            raise ValueError(f"unknown owner op {op!r}")
    return doc, changes


def find_task(tasks: list[dict], needle: str) -> dict | None:
    for t in tasks:
        if needle in "".join(t.get("anchors") or []):
            return t
    for t in tasks:
        if needle in json.dumps(t, ensure_ascii=False):
            return t
    return None


def apply_events(tasks: list[dict], events: list[tuple]) -> tuple[list[dict], list[str]]:
    """Scripted worker events: attach reports to tasks, describe them as changes."""
    changes, missing = [], []
    for ev in events:
        t = find_task(tasks, ev[1])
        if t is None:
            missing.append(ev[1])
        elif ev[0] == "finish":
            t["worker_report"] = ev[2]
            changes.append({"event": "turn_finished", "task": t["id"]})
        elif ev[0] == "quota":
            changes.append({"event": "quota_exhausted", "task": t["id"], "assistant": ev[2]})
    return changes, missing


class Ctx:
    """What a round's check may look at: the ledger snapshot the coordinator saw this round."""

    def __init__(self, ledger: list[dict], assistants: dict):
        self.ledger = ledger
        self.usable = {n for n, s in assistants.items() if s == "usable"}

    def find(self, needle: str) -> str | None:
        t = find_task(self.ledger, needle)
        return t["id"] if t else None


class Simulator:
    """Deterministic ledger + document: applies the coordinator's validated actions, dispatches workers."""

    def __init__(self) -> None:
        self.document: list[str] = []
        self.tasks: list[dict] = []
        self.ever: list[dict] = []  # every task ever created, stopped ones included

    def by_id(self, tid: str) -> dict | None:
        return next((t for t in self.tasks if t["id"] == tid), None)

    def _done(self, tid: str) -> bool:
        t = self.by_id(tid)
        return bool(t and t["state"] == "done")

    def dispatch(self) -> None:
        """Queued tasks whose dependencies are done start running; the rest block."""
        for t in self.tasks:
            if t["state"] != "queued":
                continue
            if all(self._done(d) for d in t["depends_on"]):
                t["state"] = "running"
                t["worker_session"] = f"w-{t['id']}"
                if not t.get("assistant"):
                    t["assistant"] = "claude"
            else:
                t["state"] = "blocked"

    def _add_task(self, tid: str, project: str, assistant, a: dict) -> None:
        t = {"id": tid, "state": "queued", "project": project, "assistant": assistant,
             "instruction": a.get("instruction"), "anchors": a.get("anchors") or [],
             "depends_on": a.get("depends_on") or [], "worker_session": None,
             "worker_report": None}
        self.tasks.append(t)
        self.ever.append(copy.deepcopy(t))

    def _mutate(self, a: dict) -> None:
        kind = a["type"]
        if kind == "create_task":
            self._add_task(a["id"], a["project"], a.get("assistant"), a)
        elif kind == "update_task":
            t = self.by_id(a["id"])
            t["instruction"], t["anchors"] = a.get("instruction"), a.get("anchors") or []
        elif kind == "reanchor":
            self.by_id(a["id"])["anchors"] = a.get("anchors") or []
        elif kind == "steer":
            pass  # delivered to the worker; the ledger keeps no copy
        elif kind == "stop":
            self.tasks.remove(self.by_id(a["id"]))
        elif kind == "reassign":
            t = self.by_id(a["id"])
            t["assistant"] = a["assistant"]
            t["worker_session"] = f"w-{t['id']}"
        elif kind == "mark_done":
            t = self.by_id(a["id"])
            t["state"], t["result"] = "done", a.get("summary")
        elif kind == "reopen_as_followup":
            old = self.by_id(a["of"])
            self._add_task(a["id"], old["project"], old.get("assistant"), a)
        elif kind == "ask":
            self.insert_ask(a)

    def insert_ask(self, a: dict) -> None:
        i = find_block(self.document, a["near"])
        self.document.insert(i + 1, f"<!--coordinator-->{a['text']}<!--/coordinator-->")

    def plan(self, actions: list[dict], assistants: dict) -> tuple[list[dict], list[str]]:
        """Which of these actions would execute now, in order; the rest with reasons."""
        shadow = copy.deepcopy(self)
        valid, rejected = [], []
        for a in actions:
            errs = validate([a], shadow.document, shadow.tasks, PROJECTS, assistants)
            if errs:
                rejected.extend(errs)
            else:
                shadow._mutate(a)  # so later same-round actions can reference earlier ids
                valid.append(a)
        return valid, rejected

    def commit(self, actions: list[dict]) -> None:
        for a in actions:
            self._mutate(a)


def invariant_violations(actions: list[dict], document: list[str], ever: list[dict]) -> list[str]:
    """Long-run invariants: no re-tasking anchored text, no repeated questions."""
    v = []
    for a in actions:
        if a.get("type") == "create_task":
            for t in ever:
                for na in a.get("anchors") or []:
                    for ta in t.get("anchors") or []:
                        if na in ta or ta in na:
                            v.append(f"create_task {a.get('id')!r} re-anchors text already "
                                     f"covered by {t['id']!r} ({t['state']})")
        elif a.get("type") == "ask":
            asked = (a.get("text") or "").strip()
            for block in document:
                if block.startswith(OPEN) and block.endswith(CLOSE) \
                        and block[len(OPEN):-len(CLOSE)].strip() == asked:
                    v.append("ask repeats a question already in the document")
    return v


def claude_ask(session_id: str | None, text: str, work: str,
               create: bool) -> tuple[str, dict | None, float, str | None]:
    """One claude -p call on the persistent session (create: --session-id, else --resume)."""
    cmd = ["claude", "-p", text, "--model", "haiku", "--output-format", "json",
           "--tools", "", "--permission-mode", "bypassPermissions",
           # F10: a blank session must not load the owner's ~237k-token MCP catalog
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    cmd += ["--session-id", session_id] if create else ["--resume", session_id]
    t0 = time.monotonic()
    try:
        out = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", None, time.monotonic() - t0, str(exc)
    secs = time.monotonic() - t0
    env = None
    try:
        env = json.loads(out.stdout)
    except json.JSONDecodeError:
        pass
    raw = (env or {}).get("result") or out.stdout
    if out.returncode != 0:
        error = (out.stderr or out.stdout)[-800:]
    elif env is None:
        error = "claude did not return a JSON envelope"
    elif not raw.strip():
        error = "claude exited without a response"
    else:
        error = None
    return raw, env, secs, error


def dep_pair(a: list[dict], c: Ctx) -> bool:
    cs = kinds(a, "create_task")
    sess = [t["id"] for t in cs if t["project"] == "SessKit"]
    return bool(sess) and any(t["project"] == "Notely/app-ios" and sess[0] in (t.get("depends_on") or [])
                              for t in cs)


def R(rid, expect, check, owner=(), events=(), assistants=None):
    return {"id": rid, "owner": list(owner), "events": list(events),
            "assistants": assistants or {}, "expect": expect, "check": check}


# One evolving document, ~25 rounds. Owner text is Chinese, like scenarios.py.
TIMELINE = [
    R("R01-darkmode-dispatch",
      owner=[("add", "给 Notely 网页版加一个深色模式")],
      expect="create the Notely/web dark-mode task; parallel; no ask",
      check=lambda a, c: any(t["project"] == "Notely/web" for t in kinds(a, "create_task"))),
    R("R02-csv-dispatch",
      owner=[("add", "Notely 后端做一个导出 CSV 的功能")],
      expect="create the Notely/backend CSV task in parallel; no dependency",
      check=lambda a, c: any(t["project"] == "Notely/backend" for t in kinds(a, "create_task"))
      and not any(t.get("depends_on") for t in kinds(a, "create_task"))),
    R("R03-timeout-dispatch",
      owner=[("add", "Beacon 抓取太慢了，优化一下抓取超时的处理")],
      expect="create the Beacon/backend timeout task",
      check=lambda a, c: any(t["project"] == "Beacon/backend" for t in kinds(a, "create_task"))),
    R("R04-refine-running-csv",
      owner=[("edit", "导出 CSV 的功能", "Notely 后端做一个导出 CSV 的功能，逗号和换行要正确转义")],
      expect="steer the running CSV worker; no new task",
      check=lambda a, c: any(s["id"] == c.find("导出 CSV") for s in kinds(a, "steer"))
      and not kinds(a, "create_task")),
    R("R05-half-written-line",
      owner=[("add", "然后还有那个")],
      expect="a visibly unfinished fragment: no actions at all, not even a question",
      check=lambda a, c: not a),
    R("R06-line-completed",
      owner=[("edit", "然后还有那个", "Notely 的 iOS app 启动有点慢，优化一下启动时间")],
      expect="the finished sentence becomes the Notely/app-ios task",
      check=lambda a, c: any(t["project"] == "Notely/app-ios" for t in kinds(a, "create_task"))),
    R("R07-ambiguous-login-bug",
      owner=[("add", "把那个登录 bug 修一下")],
      expect="ask which project owns the login; no task yet",
      check=lambda a, c: kinds(a, "ask") and not kinds(a, "create_task")),
    R("R08-answer-in-plain-text",
      owner=[("add_after_question", "登录 bug", "Notely 网页版的，登录后马上又跳回登录页")],
      expect="the owner's plain-text answer becomes the Notely/web task; no re-ask",
      check=lambda a, c: any(t["project"] == "Notely/web" for t in kinds(a, "create_task"))
      and not kinds(a, "ask")),
    R("R09-verified-report",
      events=[("finish", "深色模式", "Added dark mode with a system-following toggle. Ran the web unit "
                                 "tests (18 passed) and checked both themes in a browser screenshot.")],
      expect="verified worker report: mark the dark-mode task done with an exact quote",
      check=lambda a, c: any(d["id"] == c.find("深色模式") for d in kinds(a, "mark_done"))),
    R("R10-unverified-report",
      events=[("finish", "导出 CSV", "改好了。")],
      expect="unverified worker report: steer the worker to verify; not done",
      check=lambda a, c: not kinds(a, "mark_done")
      and any(s["id"] == c.find("导出 CSV") for s in kinds(a, "steer"))),
    R("R11-verified-after-steer",
      events=[("finish", "导出 CSV", "Export CSV endpoint done; commas and newlines escaped correctly. "
                                 "Ran backend tests (23 passed) and diffed an exported file with special "
                                 "characters.")],
      expect="the re-reported CSV work is verified: mark it done",
      check=lambda a, c: any(d["id"] == c.find("导出 CSV") for d in kinds(a, "mark_done"))),
    R("R12-typo-strike",
      owner=[("strike_part", "Beacon 抓取太慢了", "优化", "调整")],
      expect="a struck wording fix on a running task: no stop, no new task, no update",
      check=lambda a, c: not kinds(a, "stop") and not kinds(a, "create_task")
      and not kinds(a, "update_task")),
    R("R13-withdraw-running",
      owner=[("strike", "启动有点慢")],
      expect="the struck iOS idea withdraws its running task; the timeout task survives its typo strike",
      check=lambda a, c: any(s["id"] == c.find("启动") for s in kinds(a, "stop"))
      and not any(s["id"] == c.find("抓取超时") for s in kinds(a, "stop"))),
    R("R14-named-assistant-codex",
      owner=[("add", "让 Codex 给 Beacon 加一个 RSS 源去重")],
      expect="create the Beacon/backend RSS task on codex, as the owner named it",
      check=lambda a, c: any(t["project"] == "Beacon/backend" and t.get("assistant") == "codex"
                             for t in kinds(a, "create_task"))),
    R("R15-verified-report",
      events=[("finish", "抓取超时", "Raised the fetch timeout and added retry with backoff. Ran backend "
                                 "tests (31 passed) and verified a previously timing-out feed now "
                                 "completes.")],
      expect="verified worker report: mark the timeout task done",
      check=lambda a, c: any(d["id"] == c.find("抓取超时") for d in kinds(a, "mark_done"))),
    R("R16-followup-of-done",
      owner=[("add", "Beacon 抓取还是偶尔超时，并发高的时候更明显")],
      expect="a complaint about a done task is a follow-up of it, not a fresh task",
      check=lambda a, c: any(r["of"] == c.find("抓取超时") for r in kinds(a, "reopen_as_followup"))),
    R("R17-quota-exhausted",
      events=[("quota", "RSS", "codex")],
      assistants={"codex": "quota exhausted"},
      expect="reassign the RSS task to a usable assistant without steering or marking done",
      check=lambda a, c: any(r["id"] == c.find("RSS") and r.get("assistant") in c.usable
                             for r in kinds(a, "reassign"))
      and not kinds(a, "steer") and not kinds(a, "mark_done")),
    R("R18-dependency-pair",
      owner=[("add", "先给 SessKit 加一个任务唯一的完成标识"),
             ("add", "然后 Notely 的手机端用它来发任务完成通知")],
      expect="two tasks; the Notely/app-ios one depends on the SessKit one",
      check=dep_pair),
    R("R19-refine-blocked-task",
      owner=[("edit", "用它来发任务完成通知",
              "然后 Notely 的手机端用它来发任务完成通知，会话一结束就立刻发，别攒着")],
      expect="the notification task has not started: update_task, not steer",
      check=lambda a, c: any(u["id"] == c.find("完成通知") for u in kinds(a, "update_task"))),
    R("R20-verified-report-unblocks",
      events=[("finish", "完成标识", "Added a per-turn unique completion id to session records. Ran "
                                 "SessKit tests (12 passed) and verified two consecutive turns carry "
                                 "different ids.")],
      expect="verified worker report: mark the SessKit completion-id task done",
      check=lambda a, c: any(d["id"] == c.find("完成标识") for d in kinds(a, "mark_done"))),
    R("R21-new-project-idea",
      owner=[("add", "再做一个新工具，每天把我所有仓库的提交汇总成一份日报")],
      expect="a project that does not exist yet: ask for its name/location; no task",
      check=lambda a, c: kinds(a, "ask") and not kinds(a, "create_task")),
    R("R22-answer-new-project",
      owner=[("add_after_question", "汇总成一份日报", "就叫 Beacon Daily，放在 Beacon/backend 里")],
      expect="the named location becomes the Beacon/backend daily-report task; no re-ask",
      check=lambda a, c: any(t["project"] == "Beacon/backend" for t in kinds(a, "create_task"))
      and not kinds(a, "ask")),
    R("R23-rss-text-edited",
      owner=[("edit", "RSS 源去重", "Beacon 的 RSS 源去重，按 URL 归一化之后判断重复")],
      expect="same direction, new constraint: steer the running RSS worker; no new task, no stop",
      check=lambda a, c: any(s["id"] == c.find("RSS") for s in kinds(a, "steer"))
      and not kinds(a, "create_task") and not kinds(a, "stop")),
    R("R24-named-assistant-pi",
      owner=[("add", "用 Pi 给 Notely 网页版加一个快捷键帮助面板")],
      expect="create the Notely/web shortcut-panel task on pi, as the owner named it",
      check=lambda a, c: any(t["project"] == "Notely/web" and t.get("assistant") == "pi"
                             for t in kinds(a, "create_task"))),
    R("R25-unverified-report",
      events=[("finish", "快捷键", "做完了。")],
      expect="unverified worker report: steer the worker to verify; not done",
      check=lambda a, c: not kinds(a, "mark_done")
      and any(s["id"] == c.find("快捷键") for s in kinds(a, "steer"))),
    R("R26-verified-report",
      events=[("finish", "快捷键", "Added a '?' shortcut help panel listing every shortcut. Ran web tests "
                                 "(15 passed) and keyboard-navigated all entries.")],
      expect="the re-reported shortcut panel is verified: mark it done",
      check=lambda a, c: any(d["id"] == c.find("快捷键") for d in kinds(a, "mark_done"))),
]


def ktok(usage: dict | None) -> str:
    if not usage:
        return "-"
    n = (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0) \
        + (usage.get("cache_creation_input_tokens") or 0)
    return f"{n / 1000:.1f}k"


def run_timeline(ask_fn, restart_at: int = 0, limit: int = 0) -> list[dict]:
    sim = Simulator()
    assistants = dict(ASSISTANTS)
    session_id = None
    results = []
    with tempfile.TemporaryDirectory(prefix="butler-e6-") as work:
        for i, rnd in enumerate(TIMELINE[:limit or None], 1):
            fresh = i == 1 or (restart_at and i == restart_at)
            if fresh:
                session_id = str(uuid.uuid4())
            sim.dispatch()
            sim.document, owner_changes = apply_owner(sim.document, rnd["owner"])
            event_changes, missing = apply_events(sim.tasks, rnd["events"])
            assistants.update(rnd["assistants"])
            ctx = Ctx(copy.deepcopy(sim.tasks), assistants)
            body = round_payload(sim.document, owner_changes + event_changes,
                                 ctx.ledger, assistants)
            base = (PROMPT if fresh else "") + "\n\n## This round\n\n```json\n" + body \
                + "\n```\n\nReturn only the JSON object."
            text, attempts, total, error, obj, env = base, [], 0.0, None, None, None
            for attempt in range(2):
                raw, env, secs, error = ask_fn(session_id, text, work, fresh and attempt == 0)
                total += secs
                if not error and env and env.get("session_id"):
                    session_id = env["session_id"]
                obj = None if error else parse(raw)
                actions = (obj or {}).get("actions") or []
                rejected = [] if error or obj is None else validate(
                    actions, sim.document, sim.tasks, PROJECTS, assistants)
                attempts.append({"actions": actions, "rejected": rejected,
                                 "seconds": round(secs, 1), "error": error})
                if not error and obj is not None and not rejected:
                    break
                if error:
                    text = base  # one transport retry with the same text
                elif obj is None:
                    text = base + NOT_JSON
                else:
                    text = base + REJECTED.format(
                        actions=json.dumps({"actions": actions}, ensure_ascii=False),
                        reasons="\n- ".join(rejected))
            actions = attempts[-1]["actions"]
            judged = obj is not None and not attempts[-1]["error"]
            check_ok = bool(rnd["check"](actions, ctx)) if judged else False
            bad_quotes = quotes_exact(actions, sim.document) if judged else ["<unjudged>"]
            violations = invariant_violations(actions, sim.document, sim.ever) if judged else ["<unjudged>"]
            usage = (env or {}).get("usage")
            valid, leftover = sim.plan(actions, assistants)
            sim.commit(valid)
            ok = judged and check_ok and not bad_quotes and not violations
            results.append({
                "round": i, "id": rnd["id"], "expect": rnd["expect"],
                "session": "new" if i == 1 else ("restarted" if i == restart_at else "continued"),
                "ok": ok, "judged": judged, "check_ok": check_ok,
                "quotes_ok": not bad_quotes, "bad_quotes": bad_quotes,
                "invariants_ok": not violations, "violations": violations,
                "skipped_events": missing, "actions": actions,
                "note": (obj or {}).get("note"), "attempts": attempts,
                "first_rejected": attempts[0]["rejected"], "rejected_final": leftover,
                "seconds": round(total, 1), "usage": usage,
                "ledger_size": len(sim.tasks), "doc_blocks": len(sim.document),
                "error": attempts[-1]["error"],
            })
            flag = "ERROR" if attempts[-1]["error"] else "PASS" if ok else "FAIL"
            why = [] if ok else (["error"] if attempts[-1]["error"] else
                                 ([] if judged else ["no-json"]) +
                                 [n for n, v in (("check", check_ok),
                                                 ("quotes", not bad_quotes),
                                                 ("invariants", not violations)) if not v])
            print(f"{flag} R{i:02d} {rnd['id']:<28} {total:5.1f}s ctx={ktok(usage):>6} "
                  f"out={((usage or {}).get('output_tokens') or 0):>4} "
                  f"{json.dumps([a.get('type') for a in actions])}"
                  f"{' (' + ','.join(why) + ')' if why else ''}", flush=True)
    judged = [r for r in results if r["judged"]]
    print(f"\n{sum(r['ok'] for r in results)}/{len(results)} rounds pass"
          f" · {sum(1 for r in results if r['error'])} transport errors"
          f" · {sum(1 for r in results if r['first_rejected'])} rounds needed a rejection retry")
    print("context tokens per round: " + " ".join(
        f"R{r['round']:02d}={ktok(r['usage'])}" for r in results))
    secs = sorted(r["seconds"] for r in results)
    if secs:
        first5 = sorted(r["seconds"] for r in results[:5])
        last5 = sorted(r["seconds"] for r in results[-5:])
        print(f"latency: median {secs[len(secs) // 2]:.1f}s · first-5 median {first5[2]:.1f}s"
              f" · last-5 median {last5[2]:.1f}s · max {secs[-1]:.1f}s")
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="E6 long-running single coordinator session harness")
    ap.add_argument("--restart-at", type=int, default=0, metavar="N",
                    help="start a fresh session at round N (full prompt + current state only)")
    ap.add_argument("--limit", type=int, default=0, metavar="K",
                    help="run only the first K rounds")
    args = ap.parse_args()
    mode = f"restart{args.restart_at}" if args.restart_at else "continuous"
    results = run_timeline(claude_ask, restart_at=args.restart_at, limit=args.limit)
    out = HERE / "results" / f"e6-{mode}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"mode": mode, "restart_at": args.restart_at,
                               "assistant": "claude-haiku", "rounds": results},
                              ensure_ascii=False, indent=1))
    print(f"wrote {out}")
    return 0 if any(r["judged"] for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
