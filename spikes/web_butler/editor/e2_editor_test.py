"""E2 — editor mechanics in a real Chrome, driven through CDP with real key / IME events.

Run: python e2_editor_test.py   (needs agent-browser with Chrome, and `npm run build` done)
Prints one line per check and writes results/e2.json next to the spike.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import subprocess
import threading
import urllib.request
from pathlib import Path

import websockets

HERE = Path(__file__).resolve().parent
KEYS = {
    "Backspace": ("Backspace", 8), "Delete": ("Delete", 46), "Enter": ("Enter", 13),
    "ArrowLeft": ("ArrowLeft", 37), "ArrowRight": ("ArrowRight", 39), "End": ("End", 35),
    "ArrowDown": ("ArrowDown", 40),
}


class Page:
    def __init__(self, ws):
        self.ws, self.n, self.pending = ws, 0, {}
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            fut = self.pending.pop(msg.get("id"), None)
            if fut and not fut.done():
                fut.set_result(msg)

    async def cmd(self, method, **params):
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.n] = fut
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        msg = await fut
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg.get("result", {})

    async def js(self, expr):
        r = await self.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description"))
        return r["result"].get("value")

    async def key(self, name, modifiers=0, commands=None):
        code, vk = KEYS[name] if name in KEYS else (f"Key{name.upper()}", ord(name.upper()))
        base = {"key": name, "code": code, "windowsVirtualKeyCode": vk, "modifiers": modifiers}
        down = dict(base, type="rawKeyDown")
        if commands:
            down["commands"] = commands
        await self.cmd("Input.dispatchKeyEvent", **down)
        await self.cmd("Input.dispatchKeyEvent", **dict(base, type="keyUp"))
        await asyncio.sleep(0.03)

    async def type(self, text, delay=0.0):
        for ch in text:
            await self.cmd("Input.dispatchKeyEvent", type="keyDown", text=ch, key=ch, unmodifiedText=ch)
            await self.cmd("Input.dispatchKeyEvent", type="keyUp", key=ch)
            if delay:
                await asyncio.sleep(delay)
        await asyncio.sleep(0.03)

    async def ime(self, steps, commit):
        for s in steps:  # in-progress composition text, e.g. "n", "ni", "nih"
            await self.cmd("Input.imeSetComposition", text=s, selectionStart=len(s), selectionEnd=len(s))
            await asyncio.sleep(0.03)
        await self.cmd("Input.insertText", text=commit)
        await asyncio.sleep(0.05)


def serve() -> int:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(HERE))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


async def main() -> int:
    port = serve()
    url = f"http://127.0.0.1:{port}/index.html"
    subprocess.run(["agent-browser", "--session", "butler-e2", "open", url], check=True, capture_output=True)
    subprocess.run(["agent-browser", "--session", "butler-e2", "wait", "body[data-ready='1']"], check=True,
                   capture_output=True)
    cdp = subprocess.run(["agent-browser", "--session", "butler-e2", "get", "cdp-url"], check=True,
                         capture_output=True, text=True).stdout.strip()
    port_cdp = cdp.split(":")[2].split("/")[0]
    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port_cdp}/json"))
    ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page" and "index.html" in t["url"])
    results = []
    async with websockets.connect(ws_url, max_size=None) as ws:
        p = Page(ws)

        async def reset(md=""):
            await p.js(f"butler.load(view, {json.dumps(md)}); view.focus(); "
                       "view.dispatch({selection:{anchor:view.state.doc.length}}); true")

        async def state():
            return await p.js("({text: butler.text(view), md: butler.toMarkdown(view), strikes: butler.strikes(view),"
                              " head: view.state.selection.main.head})")

        def check(name, ok, detail):
            results.append({"check": name, "ok": bool(ok), "detail": detail})
            print(("PASS " if ok else "FAIL ") + name + "  " + json.dumps(detail, ensure_ascii=False)[:300])

        # T1 backspace x3 on text X has seen strikes, never removes
        await reset()
        await p.type("hello")
        await p.js("butler.markSeen(view); true")
        for _ in range(3):
            await p.key("Backspace")
        s = await state()
        check("T1 backspace strikes", s["text"] == "hello" and s["md"] == "he~~llo~~", s)

        # T1b typing after backspaces lands where the caret is (before the struck text)
        await p.type("y")
        s = await state()
        check("T1b type after backspace", s["md"] == "hey~~llo~~", s)

        # T2 select + overwrite
        await reset("the quick fox")
        await p.js("view.dispatch({selection:{anchor:4, head:9}}); true")
        await p.type("slow")
        s = await state()
        check("T2 overwrite = strike old + insert new", s["md"] == "the ~~quick~~slow fox", s)

        # T3 forward delete
        await reset("abcdef")
        await p.js("view.dispatch({selection:{anchor:2}}); true")
        await p.key("Delete")
        await p.key("Delete")
        s = await state()
        check("T3 forward delete strikes rightwards", s["md"] == "ab~~cd~~ef", s)

        # T4 cut
        await reset("keep cut keep")
        await p.js("view.dispatch({selection:{anchor:5, head:8}}); true")
        await p.key("x", modifiers=4, commands=["cut"])
        s = await state()
        check("T4 cut strikes (text stays)", s["md"] == "keep ~~cut~~ keep", s)

        # T5 paste over selection
        await reset("replace ME here")
        await p.js("view.dispatch({selection:{anchor:8, head:10}}); true")
        await p.cmd("Input.insertText", text="YOU")
        await asyncio.sleep(0.05)
        s = await state()
        check("T5 paste/insert over selection", s["md"] == "replace ~~ME~~YOU here", s)

        # T6 undo of a strike removes the strike
        await reset("undo me")
        await p.key("Backspace")
        s1 = await state()
        await p.key("z", modifiers=4, commands=["undo"])
        s = await state()
        check("T6 undo removes strike", s1["md"] == "undo m~~e~~" and s["md"] == "undo me",
              {"after_bs": s1, "after_undo": s})

        # T7 IME: composition edits itself freely; after commit, backspace strikes
        await reset("")
        await p.ime(["n", "ni", "nih", "niha", "nihao"], "你好")
        s1 = await state()
        await p.ime(["s", "sh", "shi"], "世")
        await p.js("butler.markSeen(view); true")
        await p.key("Backspace")
        s = await state()
        check("T7 IME commit then backspace", s1["md"] == "你好" and s["md"] == "你好~~世~~",
              {"after_ime": s1, "after_bs": s})

        # T7b IME with in-composition backspace (user corrects pinyin)
        await reset("")
        await p.ime(["z", "zh", "zho", "zh", "zho", "zhon", "zhong"], "中")
        s = await state()
        check("T7b IME self-correction leaves no strike", s["md"] == "中", s)

        # T8 locked done span rejects edits and deletions
        await reset("fix the login bug. other text")
        await p.js("butler.markDone(view, 0, 17, 't1', 'Login fixed; verified by test run.'); "
                   "view.dispatch({selection:{anchor:10}}); true")
        await p.type("ZZ")
        await p.key("Backspace")
        s = await state()
        check("T8 done span is locked", s["text"].startswith("fix the login bug."), s)

        # T9 anchors follow typing/strike before them
        await reset("first idea. second idea.")
        await p.js("butler.addAnchor(view, 'a2', 12, 23); view.dispatch({selection:{anchor:0}}); true")
        await p.type("NEW ")
        await p.js("view.dispatch({selection:{anchor:4}}); true")
        await p.key("Backspace")
        anchors = await p.js("butler.anchors(view)")
        check("T9 anchor follows text", anchors and anchors[0]["text"] == "second idea", anchors)

        # T10 agent inserts text above while the owner keeps typing below
        await reset("line one\n\nline two\n")
        await p.js("view.dispatch({selection:{anchor:view.state.doc.length}}); true")
        typed = "我正在这里连续打字 typing steadily 123"
        agent_task = p.js(
            "(async () => { for (let i = 0; i < 5; i++) { await new Promise(r => setTimeout(r, 90));"
            " butler.insertAgentText(view, 9, `[X: question ${i}?]\\n`); } return true })()")
        typing_task = p.type(typed, delay=0.02)
        await asyncio.gather(agent_task, typing_task)
        s = await state()
        check("T10 owner typing unaffected by agent inserts",
              typed in s["text"] and s["text"].count("[X: question") == 5,
              {"tail": s["text"][-60:], "md_head": s["md"][:160]})

        # T11 round trip through Markdown
        await reset("a ~~b~~ c <!--x-->ask?<!--/x--> d")
        s = await state()
        check("T11 markdown round trip",
              s["md"] == "a ~~b~~ c <!--x-->ask?<!--/x--> d" and s["text"] == "a b c ask? d", s)

        # T12 project hints
        await reset("Corral 要改一下，SessKit 也是。Go ahead and go.")
        await asyncio.sleep(0.1)
        hits = await p.js("[...document.querySelectorAll('.cm-project')].map(e => e.textContent)")
        check("T12 project hints (expect Corral, SessKit)", "Corral" in hits and "SessKit" in hits, hits)

        # T13 a done span split by a project hint still opens its result at the final
        # fragment; clicking any fragment must leave the editor caret outside it.
        await reset("Corral fixed the login bug. Other text")
        await p.js("butler.markDone(view, 0, 27, 't13', 'Verified on the real path.'); "
                   "view.dispatch({selection:{anchor:view.state.doc.length}}); true")
        before = await p.js("view.state.selection.main.head")
        first = await p.js("document.querySelector('.cm-done').getBoundingClientRect().toJSON()")
        x, y = first["left"] + first["width"] / 2, first["top"] + first["height"] / 2
        await p.cmd("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
        await p.cmd("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)
        pop = await p.js("({head:view.state.selection.main.head, visible:!document.querySelector('#popover').hidden, "
                         "left:document.querySelector('#popover').getBoundingClientRect().left, "
                         "last:[...document.querySelectorAll('[data-task=t13]')].at(-1)"
                         ".getBoundingClientRect().right})")
        check("T13 done click keeps caret and anchors to final fragment",
              pop["head"] == before and pop["visible"] and pop["left"] > pop["last"], pop)

        # T15 text X has not seen is deleted outright
        await reset()
        await p.type("draft")
        await p.key("Backspace")
        await p.key("Backspace")
        s = await state()
        check("T15 unseen text deletes for real", s["md"] == "dra" and not s["strikes"], s)

        # T16 a selection over seen + unseen text: seen part struck, unseen part removed
        await reset("seen ")
        await p.type("new")
        await p.js("view.dispatch({selection:{anchor:0, head:view.state.doc.length}}); true")
        await p.key("Backspace")
        s = await state()
        check("T16 mixed selection splits", s["md"] == "~~seen ~~", s)

        # T17 characters typed inside a seen sentence are unseen until submitted
        await reset("abc")
        await p.js("view.dispatch({selection:{anchor:1}}); true")
        await p.type("XY")
        for _ in range(3):
            await p.key("Backspace")
        s = await state()
        check("T17 new chars inside seen text delete, seen chars strike", s["md"] == "~~a~~bc", s)

        # T18 undo brings back text that was deleted outright
        await reset()
        await p.type("hello")
        await asyncio.sleep(0.6)  # edits closer than ~0.5 s share one undo step
        await p.key("Backspace")
        s1 = await state()
        await p.key("z", modifiers=4, commands=["undo"])
        s = await state()
        check("T18 undo restores an unseen deletion", s1["md"] == "hell" and s["md"] == "hello",
              {"after_bs": s1, "after_undo": s})

        # T19 overwriting unseen text replaces it; overwriting seen text strikes it
        await reset("old")
        await p.type(" tmp")
        await p.js("view.dispatch({selection:{anchor:3, head:view.state.doc.length}}); true")
        await p.type(" new")
        s1 = await state()
        await p.js("view.dispatch({selection:{anchor:0, head:3}}); true")
        await p.type("OLD")
        s = await state()
        check("T19 overwrite: unseen replaced, seen struck", s1["md"] == "old new" and s["md"] == "~~old~~OLD new",
              {"unseen": s1, "seen": s})

        # T20 backspace right after struck text skips the whole struck run
        await reset("keep ~~gone~~")
        await p.key("Backspace")
        s = await state()
        check("T20 backspace skips an already-struck run", s["head"] == 5 and s["md"] == "keep ~~gone~~", s)
        await p.key("Backspace")
        s = await state()
        check("T20b next backspace strikes before the run", s["md"] == "keep~~ gone~~", s)

        # T14 Option+Down (move line) is not bound: it would strike the line and copy it elsewhere
        await reset("first\nsecond")
        await p.js("view.dispatch({selection:{anchor:2}}); true")
        await p.key("ArrowDown", modifiers=1)
        s = await state()
        check("T14 move-line shortcut does not rewrite text", s["md"] == "first\nsecond" and not s["strikes"], s)

        await p.js("butler.load(view, 'Fix the login bug. Then add dark mode to Corral. "
                   "~~drop this~~ <!--x-->Which project for dark mode?<!--/x-->'); "
                   "butler.markDone(view, 0, 18, 't1', 'Done: login fixed in Notely/web.\\n"
                   "Verified: test run + screenshot.'); true")
    subprocess.run(["agent-browser", "--session", "butler-e2", "screenshot", str(HERE / "results" / "e2.png")],
                   capture_output=True)
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "e2.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"{sum(r['ok'] for r in results)}/{len(results)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
