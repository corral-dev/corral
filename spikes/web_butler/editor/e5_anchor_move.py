"""E5: real-browser cut/paste probe for task-anchor identity.

The pass bar is that an anchor on cut text moves to the pasted live copy while the struck
source remains visible. This deliberately exposes a gap in the E2 range-only prototype.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import urllib.request
from pathlib import Path

import websockets

from e2_editor_test import Page, serve

HERE = Path(__file__).resolve().parent
SESSION = "butler-e5"
SOURCE = "Move this idea."
INITIAL = f"Alpha. {SOURCE} Omega."


async def main() -> int:
    port = serve()
    subprocess.run(["agent-browser", "--session", SESSION, "open", f"http://127.0.0.1:{port}/index.html"],
                   check=True, capture_output=True)
    subprocess.run(["agent-browser", "--session", SESSION, "wait", "body[data-ready='1']"],
                   check=True, capture_output=True)
    cdp = subprocess.run(["agent-browser", "--session", SESSION, "get", "cdp-url"],
                         check=True, capture_output=True, text=True).stdout.strip()
    port_cdp = cdp.split(":")[2].split("/")[0]
    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port_cdp}/json"))
    ws_url = next(t["webSocketDebuggerUrl"] for t in targets
                  if t.get("type") == "page" and "index.html" in t["url"])
    try:
        async with websockets.connect(ws_url, max_size=None) as ws:
            p = Page(ws)
            await p.js(f"butler.load(view, {json.dumps(INITIAL)}); "
                       f"butler.addAnchor(view, 'move', {INITIAL.index(SOURCE)}, "
                       f"{INITIAL.index(SOURCE) + len(SOURCE)}); view.focus(); true")
            start = INITIAL.index(SOURCE)
            await p.js(f"view.dispatch({{selection:{{anchor:{start},head:{start + len(SOURCE)}}}}}); true")
            await p.key("x", modifiers=4, commands=["cut"])
            after_cut = await p.js("({text:butler.text(view), md:butler.toMarkdown(view), anchors:butler.anchors(view)})")
            await p.js("view.dispatch({selection:{anchor:view.state.doc.length}}); true")
            await p.key("v", modifiers=4, commands=["paste"])
            result = await p.js("({text:butler.text(view), md:butler.toMarkdown(view), anchors:butler.anchors(view)})")
            await p.key("z", modifiers=4, commands=["undo"])
            after_undo = await p.js("({text:butler.text(view), md:butler.toMarkdown(view), anchors:butler.anchors(view)})")
            await p.key("z", modifiers=12, commands=["redo"])  # Meta+Shift
            after_redo = await p.js("({text:butler.text(view), md:butler.toMarkdown(view), anchors:butler.anchors(view)})")
    finally:
        subprocess.run(["agent-browser", "--session", SESSION, "close"], capture_output=True)

    expected = result["text"].rfind(SOURCE)
    pasted = expected > start and result["text"].endswith(SOURCE)
    anchored_to_paste = pasted and len(result["anchors"]) == 1 and result["anchors"][0]["from"] == expected
    undo_ok = after_undo["text"] == after_cut["text"] and len(after_undo["anchors"]) == 1 \
        and after_undo["anchors"][0]["from"] == start
    redo_ok = after_redo["text"] == result["text"] and len(after_redo["anchors"]) == 1 \
        and after_redo["anchors"][0]["from"] == expected
    evidence = {"pass": anchored_to_paste and undo_ok and redo_ok, "clipboard_paste_worked": pasted,
                "anchor_move_ok": anchored_to_paste, "undo_ok": undo_ok, "redo_ok": redo_ok,
                "expected_anchor_from": expected, "after_cut": after_cut, "after_paste": result,
                "after_undo": after_undo, "after_redo": after_redo}
    out = HERE.parent / "results" / "e5-anchor-move.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(("PASS" if evidence["pass"] else "FAIL") + " anchor follows cut/paste and undo/redo")
    print(json.dumps({"paste_worked": pasted, "anchor_from": result["anchors"][0]["from"],
                      "expected": expected, "undo_ok": undo_ok, "redo_ok": redo_ok}, ensure_ascii=False))
    print(out)
    return 0 if evidence["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
