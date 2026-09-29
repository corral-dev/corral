"""E3 — X judgement evaluation. Runs every scenario through an assistant in one-shot mode.

Usage: python run_eval.py claude-haiku|codex-luna [scenario-id-prefix ...]
Scores: scenario check (behaviour) + quote exactness (every anchor/quote/near must be copied
verbatim from the document's plain text) + latency. Writes results/e3-<assistant>.json.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from scenarios import ASSISTANTS, PROJECTS, SCENARIOS

HERE = Path(__file__).resolve().parent
PROMPT = (HERE / "x_prompt.md").read_text()

ACTION_FIELDS = {
    "create_task": {"id": "string", "project": "string", "assistant": "nullable_string",
                    "instruction": "string", "anchors": "strings", "depends_on": "strings"},
    "update_task": {"id": "string", "instruction": "string", "anchors": "strings"},
    "steer": {"id": "string", "message": "string", "interrupt": "boolean"},
    "stop": {"id": "string", "reason": "string"},
    "reassign": {"id": "string", "assistant": "string", "instruction": "string", "reason": "string"},
    "ask": {"near": "string", "text": "string"},
    "mark_done": {"id": "string", "quote": "string", "summary": "string"},
    "reopen_as_followup": {"of": "string", "id": "string", "instruction": "string", "anchors": "strings"},
}


def output_schema() -> dict:
    scalar = {"string": {"type": "string"}, "nullable_string": {"type": ["string", "null"]},
              "boolean": {"type": "boolean"}, "strings": {"type": "array", "items": {"type": "string"}}}
    actions = []
    for name, fields in ACTION_FIELDS.items():
        props = {"type": {"type": "string", "enum": [name]}}
        props.update({key: scalar[kind] for key, kind in fields.items()})
        actions.append({"type": "object", "properties": props, "required": list(props),
                        "additionalProperties": False})
    return {"type": "object", "properties": {
        "actions": {"type": "array", "items": {"anyOf": actions}}, "note": {"type": "string"}},
        "required": ["actions", "note"], "additionalProperties": False}


def plain(text: str) -> str:
    return re.sub(r"<!--/?x-->", "", text).replace("~~", "")


def payload(s: dict) -> str:
    doc = [{"block": i + 1, "text": t} for i, t in enumerate(s["document"])]
    return json.dumps({"document": doc, "changes": s["changes"], "ledger": s["ledger"],
                       "projects": PROJECTS, "assistants": ASSISTANTS}, ensure_ascii=False, indent=1)


def ask(assistant: str, text: str) -> tuple[str, float, str | None]:
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="butler-x-eval-") as work:
        if assistant == "claude-haiku":
            cmd = ["claude", "-p", text, "--model", "haiku", "--output-format", "json",
                   "--tools", "", "--permission-mode", "bypassPermissions",
                   "--no-session-persistence"]
            out = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=180)
            try:
                raw = json.loads(out.stdout).get("result", "")
            except json.JSONDecodeError:
                raw = out.stdout
        elif assistant == "codex-luna":
            answer_file = Path(work) / "answer.txt"
            schema_file = Path(work) / "schema.json"
            schema_file.write_text(json.dumps(output_schema()))
            cmd = ["codex", "exec", "--skip-git-repo-check", "--ephemeral",
                   "-s", "read-only", "-m", "gpt-6-luna",
                   "-c", 'model_reasoning_effort="medium"',
                   "--output-schema", str(schema_file),
                   "--output-last-message", str(answer_file), text]
            out = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=180)
            raw = answer_file.read_text() if answer_file.exists() else ""
        else:
            raise ValueError(f"unknown assistant {assistant}")
    error = None if out.returncode == 0 else (out.stderr or out.stdout)[-800:]
    if not raw.strip() and error is None:
        error = "assistant exited without a response"
    return raw, time.monotonic() - t0, error


def parse(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def quotes_exact(actions: list[dict], document: list[str]) -> list[str]:
    text = "\n".join(plain(t) for t in document)
    bad = []
    for a in actions:
        for q in (a.get("anchors") or []) + [a.get("quote"), a.get("near")]:
            if q and q not in text:
                bad.append(q)
    return bad


def run_one(assistant: str, s: dict) -> dict:
    text = PROMPT + "\n\n## This round\n\n```json\n" + payload(s) + "\n```\n\nReturn only the JSON object."
    try:
        raw, secs, error = ask(assistant, text)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raw, secs, error = "", 0, str(exc)
    obj = parse(raw)
    actions = (obj or {}).get("actions") or []
    ok = bool(obj) and bool(s["check"](actions)) if not error else False
    bad = quotes_exact(actions, s["document"])
    return {"id": s["id"], "expect": s["expect"], "behaviour_ok": ok, "quotes_ok": not bad, "bad_quotes": bad,
            "seconds": round(secs, 1), "actions": actions, "note": (obj or {}).get("note"),
            "error": error, "raw": None if obj else raw[:800]}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("claude-haiku", "codex-luna"):
        raise SystemExit("Usage: python run_eval.py claude-haiku|codex-luna [scenario-id-prefix ...]")
    assistant = sys.argv[1]
    prefixes = sys.argv[2:]
    chosen = [s for s in SCENARIOS if not prefixes or any(s["id"].startswith(p) for p in prefixes)]
    if not chosen:
        raise SystemExit("no scenarios selected")
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda s: run_one(assistant, s), chosen))
    for r in results:
        flag = "ERROR" if r["error"] else "PASS" if r["behaviour_ok"] and r["quotes_ok"] else "FAIL"
        print(f"{flag} {r['id']:<30} {r['seconds']:>5}s  behaviour={r['behaviour_ok']} quotes={r['quotes_ok']}  "
              f"{json.dumps([a.get('type') for a in r['actions']])} {r['error'] or ''}")
    label = "full" if not prefixes else "-".join(prefixes)
    out = HERE / "results" / f"e3-{assistant}-{label}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    valid = [r for r in results if not r["error"]]
    ok = sum(r["behaviour_ok"] and r["quotes_ok"] for r in valid)
    secs = sorted(r["seconds"] for r in valid)
    latency = f" · latency median {secs[len(secs) // 2]}s max {secs[-1]}s" if secs else ""
    print(f"{ok}/{len(valid)} valid responses pass; {len(results)-len(valid)} transport errors{latency}")
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
