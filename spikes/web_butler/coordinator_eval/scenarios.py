"""E3 scenarios: messy owner writing → what a good coordinator does.

Project names are fictional except Corral/SessKit.
"""

PROJECTS = {
    "Corral": ["Corral/cli", "Corral/ios"],
    "SessKit": ["SessKit"],
    "Notely": ["Notely/web", "Notely/backend", "Notely/app-ios"],
    "Beacon": ["Beacon/backend"],
}
ASSISTANTS = {"claude": "usable", "codex": "usable", "pi": "usable",
              "cursor": "quota exhausted", "kimi": "not logged in"}


def task(id, state, project, anchors, assistant="claude", report=None, deps=None):
    return {"id": id, "state": state, "project": project, "assistant": assistant, "anchors": anchors,
            "depends_on": deps or [], "worker_report": report}


def kinds(actions, kind):
    return [a for a in actions if a.get("type") == kind]


SCENARIOS = [
    {
        "id": "S1-two-projects-parallel",
        "document": ["Corral 手机端会话列表加一个搜索框，顺便把 Notely 导出 PDF 乱码修一下"],
        "changes": [{"block": 1, "kind": "added"}],
        "ledger": [],
        "expect": "Corral/ios task; Notely task or a question about which Notely part; no dependency",
        "check": lambda a: any(t["project"] == "Corral/ios" for t in kinds(a, "create_task"))
        and (any(t["project"].startswith("Notely") for t in kinds(a, "create_task")) or kinds(a, "ask"))
        and not any(t.get("depends_on") for t in kinds(a, "create_task")),
    },
    {
        "id": "S2-dependency",
        "document": ["先给 SessKit 加一个每轮唯一的完成标识，然后 Corral 手机端用它来做完成通知去重"],
        "changes": [{"block": 1, "kind": "added"}],
        "ledger": [],
        "expect": "two tasks, Corral one depends on SessKit one",
        "check": lambda a: len(kinds(a, "create_task")) == 2
        and any(t.get("depends_on") for t in kinds(a, "create_task") if t["project"].startswith("Corral")),
    },
    {
        "id": "S3-ambiguous-project",
        "document": ["把那个登录 bug 修了"],
        "changes": [{"block": 1, "kind": "added"}],
        "ledger": [],
        "expect": "ask which project; no task",
        "check": lambda a: kinds(a, "ask") and not kinds(a, "create_task"),
    },
    {
        "id": "S4-half-written",
        "document": ["Corral 的 README 已经更新了。", "然后还有那个"],
        "changes": [{"block": 2, "kind": "added"}],
        "ledger": [],
        "expect": "nothing yet",
        "check": lambda a: not a,
    },
    {
        "id": "S5-typo-strike",
        "document": ["Corral 列表加个~~索~~搜索框"],
        "changes": [{"block": 1, "kind": "edited", "before": "Corral 列表加个索",
                     "after": "Corral 列表加个~~索~~搜索框"}],
        "ledger": [task("t1", "running", "Corral/ios", ["Corral 列表加个索"])],
        "expect": "no stop, no new task (typo fix)",
        "check": lambda a: not kinds(a, "stop") and not kinds(a, "create_task")
        and not kinds(a, "update_task"),
    },
    {
        "id": "S6-withdraw-running",
        "document": ["Corral 手机端加搜索。", "~~给 Notely 网页版加暗色模式~~"],
        "changes": [{"block": 2, "kind": "struck", "before": "给 Notely 网页版加暗色模式"}],
        "ledger": [task("t2", "running", "Notely/web", ["给 Notely 网页版加暗色模式"])],
        "expect": "stop t2",
        "check": lambda a: any(s.get("id") == "t2" for s in kinds(a, "stop")),
    },
    {
        "id": "S7-steer-running",
        "document": ["给 Corral 手机端加深色模式", "深色模式要能跟随系统自动切换"],
        "changes": [{"block": 2, "kind": "added"}],
        "ledger": [task("t3", "running", "Corral/ios", ["给 Corral 手机端加深色模式"])],
        "expect": "steer t3, no new task",
        "check": lambda a: any(s.get("id") == "t3" for s in kinds(a, "steer")) and not kinds(a, "create_task"),
    },
    {
        "id": "S8-followup-of-done",
        "document": ["修好 Notely 导出 PDF 中文乱码", "Notely 导出 PDF 还是乱码，中文字体好像没嵌进去"],
        "changes": [{"block": 2, "kind": "added"}],
        "ledger": [task("t4", "done", "Notely/backend", ["修好 Notely 导出 PDF 中文乱码"],
                        report="Switched PDF font to Noto Sans; tests pass.")],
        "expect": "follow-up of t4",
        "check": lambda a: any(r.get("of") == "t4" for r in kinds(a, "reopen_as_followup")),
    },
    {
        "id": "S9-new-project",
        "document": ["新做一个小工具，每天把我所有仓库的 git 提交汇总成一份日报"],
        "changes": [{"block": 1, "kind": "added"}],
        "ledger": [],
        "expect": "ask for name/location; no task",
        "check": lambda a: kinds(a, "ask") and not kinds(a, "create_task"),
    },
    {
        "id": "S10-assistant-named",
        "document": ["让 Codex 给 Beacon 加一个 RSS 源去重"],
        "changes": [{"block": 1, "kind": "added"}],
        "ledger": [],
        "expect": "task on Beacon/backend with codex",
        "check": lambda a: any(t.get("assistant") == "codex" and t["project"] == "Beacon/backend"
                               for t in kinds(a, "create_task")),
    },
    {
        "id": "S11-named-assistant-unusable",
        "document": ["用 Cursor 把 Corral 的 README 错别字改一下"],
        "changes": [{"block": 1, "kind": "added"}],
        "ledger": [],
        "expect": "never dispatch to cursor",
        "check": lambda a: not any(t.get("assistant") == "cursor" for t in kinds(a, "create_task")),
    },
    {
        "id": "S12-answer-in-text",
        "document": ["登录后立刻跳回登录页，把这个 bug 修了",
                     "<!--coordinator-->是哪个项目的登录？<!--/coordinator-->", "Notely 网页版的"],
        "changes": [{"block": 3, "kind": "added"}],
        "ledger": [],
        "expect": "task on Notely/web",
        "check": lambda a: any(t["project"] == "Notely/web" for t in kinds(a, "create_task")),
    },
    {
        "id": "S13-verified-report",
        "document": ["Corral 手机端会话列表加搜索框"],
        "changes": [{"event": "turn_finished", "task": "t5"}],
        "ledger": [task("t5", "running", "Corral/ios", ["Corral 手机端会话列表加搜索框"],
                        report="Added a search field to the session list. Ran the iOS unit tests (42 passed) and "
                               "checked a device screenshot: typing filters the list.")],
        "expect": "mark t5 done with an exact quote",
        "check": lambda a: any(d.get("id") == "t5" for d in kinds(a, "mark_done")),
    },
    {
        "id": "S14-unverified-report",
        "document": ["修一下 Beacon 抓取超时"],
        "changes": [{"event": "turn_finished", "task": "t6"}],
        "ledger": [task("t6", "running", "Beacon/backend", ["修一下 Beacon 抓取超时"], report="改好了。")],
        "expect": "steer to verify; not done",
        "check": lambda a: not kinds(a, "mark_done") and any(s.get("id") == "t6" for s in kinds(a, "steer")),
    },
    {
        "id": "S15-quota-exhausted",
        "document": ["给 Notely 后端加导出 CSV"],
        "changes": [{"event": "quota_exhausted", "task": "t7", "assistant": "cursor"}],
        "ledger": [task("t7", "running", "Notely/backend", ["给 Notely 后端加导出 CSV"], assistant="cursor")],
        "expect": "reassign t7 to a usable assistant without steering Cursor",
        "check": lambda a: any(t.get("id") == "t7" and t.get("assistant") in ("claude", "codex", "pi")
                               for t in kinds(a, "reassign"))
        and not kinds(a, "steer") and not kinds(a, "mark_done"),
    },
    {
        "id": "S16-vague-bug-still-needs-detail",
        "document": ["把那个登录 bug 修了",
                     "<!--coordinator-->是哪个项目的登录？<!--/coordinator-->", "Notely 网页版的"],
        "changes": [{"block": 3, "kind": "added"}],
        "ledger": [],
        "expect": "ask for the symptom because the owner has not identified a particular bug",
        "check": lambda a: len(kinds(a, "ask")) == 1 and not kinds(a, "create_task")
        and any(word in kinds(a, "ask")[0]["text"].lower()
                for word in ("现象", "表现", "复现", "发生", "报错", "错误", "symptom", "repro", "what happens"))
        and not any(word in kinds(a, "ask")[0]["text"].lower()
                    for word in ("哪个组件", "哪个项目", "web or backend", "which component", "which project")),
    },
]
