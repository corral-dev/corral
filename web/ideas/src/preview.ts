// Preview build only: seed content and a rule-based stand-in for the coordinator so the
// page can be tried without the Corral service. Real judgement belongs to the coordinator
// agent (design §6.2); these rules only exercise the interface.
import { dayKey, shiftDay } from "./dates";
import { lang } from "./i18n";
import { DaySection, Message, Task, Thread, newId, openQuestion } from "./model";
import type { State, Store } from "./store";

const PROJECTS = ["Corral", "Notely", "Beacon", "SessKit", "Grove"];
const ASSISTANTS = ["Codex", "Claude Code", "Pi", "OpenCode", "Cursor"];
const zh = lang === "zh";

const copy = {
  whichProject: zh
    ? "这是哪个项目的？出问题时具体看到了什么？"
    : "Which project is this for, and what exactly happens when it goes wrong?",
  starting: (a: string, p: string) => (zh ? `交给 ${a} 在 ${p} 里处理，完成后我会核对结果。` : `${a} is taking this in ${p}. I'll check the result when it's done.`),
  steer: (a: string) => (zh ? `收到，已经转告 ${a}。` : `Got it. I've passed that on to ${a}.`),
  followUp: (a: string) => (zh ? `好的，作为后续任务交给 ${a}。` : `Sure. ${a} is picking this up as a follow-up.`),
  stopped: zh ? "好的，已经停下，不再继续。" : "Okay, I've stopped it.",
  thanks: (p: string, a: string) => (zh ? `明白，是 ${p}。现在交给 ${a}。` : `Thanks, that's ${p}. ${a} is starting now.`),
  summaryWorking: (p: string) => (zh ? `正在 ${p} 里处理。` : `In progress in ${p}.`),
  summaryWaiting: zh ? "先确认项目和具体现象。" : "Confirming the project and what actually happens.",
  summaryDone: zh ? "已完成并核对。" : "Done and checked.",
  summaryStopped: zh ? "按你的要求停下了。" : "Stopped at your request.",
  resultSummary: (p: string) => (zh ? `改动已完成，在 ${p} 里可以直接试。` : `The change is in place; you can try it in ${p}.`),
  resultEvidence: zh ? "按原问题的场景重新走了一遍，并跑了项目自带的测试，全部通过。" : "Re-ran the original scenario and the project's own tests; all passed.",
};

const STOP = /\b(stop|cancel|never ?mind|forget it)\b|算了|不做|取消|先不/i;

function findProject(text: string): string | undefined {
  const lower = text.toLowerCase();
  return PROJECTS.find((p) => lower.includes(p.toLowerCase()));
}

let assistantTurn = 0;
function pickAssistant(): string {
  assistantTurn = (assistantTurn + 1) % ASSISTANTS.length;
  return ASSISTANTS[assistantTurn];
}

function task(title: string, project: string, assistant: string, state: Task["state"]): Task {
  return { id: newId("t"), title, project, assistant, state, updatedAt: Date.now() };
}

/** Longest run of free text inside [from, to), trimmed; spans and blanks excluded. */
function freeRange(store: Store, day: string, from: number, to: number): { from: number; to: number } | null {
  const s = store.section(day);
  if (!s) return null;
  let best: { from: number; to: number } | null = null;
  let start = from;
  const cuts = s.spans.filter((sp) => sp.to > from && sp.from < to).sort((a, b) => a.from - b.from);
  const segs: [number, number][] = [];
  for (const sp of cuts) {
    segs.push([start, Math.max(start, sp.from)]);
    start = Math.max(start, sp.to);
  }
  segs.push([start, to]);
  for (let [a, b] of segs) {
    while (a < b && /\s/.test(s.text[a])) a++;
    while (b > a && /\s/.test(s.text[b - 1])) b--;
    if (b > a && (!best || b - a > best.to - best.from)) best = { from: a, to: b };
  }
  return best;
}

function looksUnfinished(text: string): boolean {
  const t = text.trim();
  return [...t].length < 4 || /(\.\.\.|…|，|,|、|：|:)$/.test(t);
}

export class PreviewCoordinator {
  private timers = new Map<string, number[]>();
  constructor(private store: Store) {
    // Resume simulated work left running in a previous visit.
    for (const th of Object.values(store.state.threads)) {
      const running = th.tasks.find((t) => t.state === "running" || t.state === "queued");
      if (running && Date.now() - running.updatedAt > 5 * 60_000) continue;
      if (running) this.runToCompletion(th.id, running.id, 20_000);
    }
  }

  onSettled(day: string, from: number, to: number): void {
    const range = freeRange(this.store, day, from, to);
    if (!range) return;
    const reading = { day, ...range };
    this.store.setReading(reading, true);
    window.setTimeout(() => {
      this.store.setReading(reading, false);
      const s = this.store.section(day);
      if (!s) return;
      const text = s.text.slice(range.from, range.to);
      if (looksUnfinished(text)) return;
      const project = findProject(text);
      if (!project) {
        const qid = newId("q");
        const id = this.store.takeUp(day, range.from, range.to, {
          summary: copy.summaryWaiting,
          tasks: [],
          questions: [{ id: qid, state: "open", blocking: false }],
          messages: [msg("coordinator", "question", copy.whichProject, qid)],
        });
        void id;
        return;
      }
      const assistant = pickAssistant();
      const t = task(text, project, assistant, "queued");
      const id = this.store.takeUp(day, range.from, range.to, {
        summary: copy.summaryWorking(project),
        tasks: [t],
        questions: [],
        messages: [msg("coordinator", "text", copy.starting(assistant, project))],
      });
      if (id) this.runToCompletion(id, t.id, 22_000);
    }, 1400);
  }

  /**
   * The owner commented. `target` is the top-level comment being replied under (and whose
   * author is addressed); without it the owner started a new top-level comment.
   */
  onReply(threadId: string, text: string, target?: { parentId: string; replyTo?: Message["author"] }): void {
    const own = this.store.addMessage(threadId, { author: "owner", kind: "text", text, parentId: target?.parentId, replyTo: target?.replyTo });
    const th = this.store.state.threads[threadId];
    if (!th) return;
    const q = openQuestion(th);
    if (q) this.store.updateThread(threadId, (x) => ({ ...x, questions: x.questions.map((y) => (y.id === q.id ? { ...y, state: "answered" } : y)) }));
    // The coordinator answers in the same group: under the comment replied to, or under the new comment.
    // Answering a reply inside an existing group addresses the owner ("@you"); answering the
    // owner's own new comment is a direct reply and needs no address.
    const group = target?.parentId ?? own;
    const address = target !== undefined;
    this.later(threadId, 1300, () => this.respond(threadId, text, group, address));
  }

  private say(threadId: string, text: string, group: string, address: boolean): void {
    this.store.addMessage(threadId, { author: "coordinator", kind: "text", text, parentId: group, replyTo: address ? "owner" : undefined });
  }

  private respond(threadId: string, text: string, group: string, address: boolean): void {
    const th = this.store.state.threads[threadId];
    if (!th) return;
    const live = th.tasks.find((t) => !["done", "failed", "canceled"].includes(t.state));
    if (STOP.test(text)) {
      this.clear(threadId);
      this.store.updateThread(threadId, (x) => ({
        ...x,
        summary: copy.summaryStopped,
        questions: x.questions.map((q) => (q.state === "open" || q.state === "answered" ? { ...q, state: "withdrawn" } : q)),
        tasks: x.tasks.map((t) => (["done", "failed", "canceled"].includes(t.state) ? t : { ...t, state: "canceled", updatedAt: Date.now() })),
      }));
      this.say(threadId, copy.stopped, group, address);
      return;
    }
    const q = openQuestion(th);
    if (q) {
      const project = findProject(text) ?? findProject(th.quote) ?? text.trim().split(/\s+/)[0];
      const assistant = pickAssistant();
      const t = task(th.quote, project, assistant, "running");
      this.store.updateThread(threadId, (x) => ({
        ...x,
        summary: copy.summaryWorking(project),
        questions: x.questions.map((y) => (y.id === q.id ? { ...y, state: "resolved" } : y)),
        tasks: [...x.tasks, t],
      }));
      this.say(threadId, copy.thanks(project, assistant), group, address);
      this.runToCompletion(threadId, t.id, 20_000);
      return;
    }
    if (live) {
      this.say(threadId, copy.steer(live.assistant ?? ""), group, address);
      return;
    }
    const prev = th.tasks[th.tasks.length - 1];
    const project = findProject(text) ?? prev?.project ?? PROJECTS[0];
    const assistant = prev?.assistant ?? pickAssistant();
    const t = task(text, project, assistant, "running");
    this.store.updateThread(threadId, (x) => ({ ...x, summary: copy.summaryWorking(project), tasks: [...x.tasks, t] }));
    this.say(threadId, copy.followUp(assistant), group, address);
    this.runToCompletion(threadId, t.id, 18_000);
  }

  private setTask(threadId: string, taskId: string, state: Task["state"]): void {
    this.store.updateThread(threadId, (x) => ({
      ...x,
      tasks: x.tasks.map((t) => (t.id === taskId ? { ...t, state, updatedAt: Date.now() } : t)),
    }));
  }

  private runToCompletion(threadId: string, taskId: string, ms: number): void {
    const started = Date.now();
    this.later(threadId, 1200, () => this.setTask(threadId, taskId, "running"));
    this.later(threadId, ms, () => this.setTask(threadId, taskId, "review"));
    this.later(threadId, ms + 3500, () => {
      const th = this.store.state.threads[threadId];
      const t = th?.tasks.find((x) => x.id === taskId);
      if (!th || !t || t.state !== "review") return;
      this.setTask(threadId, taskId, "done");
      this.store.updateThread(threadId, (x) => ({ ...x, summary: copy.summaryDone }));
      this.store.addMessage(threadId, {
        author: "coordinator",
        kind: "result",
        text: "",
        result: {
          summary: copy.resultSummary(t.project ?? ""),
          evidence: copy.resultEvidence,
          assistant: t.assistant ?? "",
          project: t.project ?? "",
          minutes: Math.max(1, Math.round((Date.now() - started) / 60000)),
        },
      });
    });
  }

  private later(threadId: string, ms: number, fn: () => void): void {
    const list = this.timers.get(threadId) ?? [];
    list.push(window.setTimeout(fn, ms));
    this.timers.set(threadId, list);
  }

  private clear(threadId: string): void {
    for (const id of this.timers.get(threadId) ?? []) clearTimeout(id);
    this.timers.delete(threadId);
  }
}

function msg(author: Message["author"], kind: Message["kind"], text: string, questionId?: string): Message {
  return { id: newId("m"), author, at: Date.now(), kind, text, questionId };
}

// ---- seed -------------------------------------------------------------------------------

interface SeedItem {
  sentence: string;
  build: (at: number) => Omit<Thread, "id" | "day" | "quote" | "createdAt" | "lastReadAt"> & { read: boolean };
}

function result(summary: string, evidence: string, assistant: string, project: string, minutes: number, at: number): Message {
  return { id: newId("m"), author: "coordinator", at, kind: "result", text: "", result: { summary, evidence, assistant, project, minutes } };
}

function say(author: Message["author"], text: string, at: number, kind: Message["kind"] = "text", questionId?: string): Message {
  return { id: newId("m"), author, at, kind, text, questionId };
}

/** A top-level comment followed by replies in its group (forum layout). */
function thread(root: Message, ...replies: Message[]): Message[] {
  const out = [root];
  replies.forEach((r, i) => {
    // The first reply answers the comment itself; later ones answer the reply before them.
    const prev = replies[i - 1];
    out.push({ ...r, parentId: root.id, replyTo: prev && prev.author !== r.author ? prev.author : undefined });
  });
  return out;
}

function seedTask(title: string, project: string, assistant: string, state: Task["state"], at: number): Task {
  return { id: newId("t"), title, project, assistant, state, updatedAt: at };
}

function seedDays(): { offset: number; extra?: string; items: (SeedItem | string)[] }[] {
  const min = 60_000;
  if (zh) {
    return [
      {
        offset: 0,
        items: [
          {
            sentence: "Corral 手机端会话很多的时候，列表滑动有点卡。",
            build: (at) => ({
              read: true,
              summary: "在 iPhone 上复现滑动卡顿，定位列表渲染的开销。",
              questions: [],
              tasks: [seedTask("复现并修复列表滑动卡顿", "Corral/ios", "Codex", "running", at + 2 * min)],
              messages: [say("coordinator", "交给 Codex 先在 iPhone 上用 300 条会话复现，再看列表渲染。完成后我会核对结果。", at + 2 * min)],
            }),
          },
          {
            sentence: "Notely 导出 PDF 时中文偶尔变成方块，修一下，用真实文件验证。",
            build: (at) => ({
              read: false,
              summary: "已完成并核对。",
              questions: [],
              tasks: [seedTask("修复 PDF 导出中文缺字", "Notely", "Claude Code", "done", at + 30 * min)],
              messages: [
                say("coordinator", "交给 Claude Code 在 Notely 里处理，完成后我会核对结果。", at + min),
                result(
                  "导出时改为内嵌中文字体子集，缺字问题已修复。",
                  "用 3 份含中英混排的真实文档导出 PDF，逐页核对没有方块；导出相关的 18 项测试通过。",
                  "Claude Code",
                  "Notely",
                  29,
                  at + 31 * min,
                ),
              ],
            }),
          },
          {
            sentence: "登录之后有时会被踢回登录页",
            build: (at) => {
              const qid = newId("q");
              return {
                read: false,
                summary: "先确认项目和具体现象。",
                tasks: [],
                questions: [{ id: qid, state: "open", blocking: true }],
                messages: [say("coordinator", "这是哪个项目的？大概登录多久之后被踢回？是所有设备都会，还是只有某一台？", at + min, "question", qid)],
              };
            },
          },
        ],
      },
      {
        offset: -1,
        items: [
          {
            sentence: "SessKit 里 Codex 会话的标题有时是空的，查一下原因。",
            build: (at) => ({
              read: true,
              summary: "已完成并核对。",
              questions: [],
              tasks: [seedTask("查清 Codex 空标题原因", "SessKit", "Pi", "done", at + 40 * min)],
              messages: [
                say("coordinator", "交给 Pi 在 SessKit 里排查。", at + min),
                result("新版 Codex 把标题写进了另一条记录，读取时补上了这一路。", "用 12 份真实 Codex 历史回放，标题全部正确；解析测试新增 3 项并通过。", "Pi", "SessKit", 38, at + 41 * min),
              ],
            }),
          },
          {
            sentence: "整理一下 Beacon 的 README。",
            build: (at) => ({
              read: true,
              summary: "按你的要求停下了。",
              questions: [],
              tasks: [seedTask("整理 README", "Beacon", "OpenCode", "canceled", at + 20 * min)],
              messages: thread(
                say("coordinator", "交给 OpenCode 在 Beacon 里处理。", at + min),
                say("owner", "这个先不做了。", at + 18 * min),
                say("coordinator", "好的，已经停下，不再继续。", at + 19 * min),
              ),
            }),
          },
        ],
      },
      {
        offset: -3,
        items: [
          {
            sentence: "Grove 首页打开太慢，看看能不能把首屏压到一秒以内。",
            build: (at) => ({
              read: true,
              summary: "已完成并核对。",
              questions: [],
              tasks: [seedTask("首屏加载优化", "Grove", "Codex", "done", at + 55 * min)],
              messages: [
                say("coordinator", "交给 Codex 在 Grove 里处理。", at + min),
                result("拆掉了首屏不需要的大依赖，并预取关键数据。", "在限速 4G 下实测 5 次，首屏中位数 0.8 秒（原来 2.6 秒）。", "Codex", "Grove", 52, at + 56 * min),
              ],
            }),
          },
          "周末想想 Ideas 页面要不要支持手机上直接写。",
        ],
      },
      {
        offset: -10,
        items: [
          {
            sentence: "Beacon 的订阅源超时之后要能自动重试。",
            build: (at) => ({
              read: true,
              summary: "已完成并核对。",
              questions: [],
              tasks: [seedTask("超时自动重试", "Beacon", "Claude Code", "done", at + 25 * min)],
              messages: [result("超时后按退避自动重试三次，失败才标记为不可用。", "模拟 5 种超时场景，全部按预期重试；相关测试 9 项通过。", "Claude Code", "Beacon", 24, at + 26 * min)],
            }),
          },
          {
            sentence: "Corral 中继断开时，手机上的提示要说清楚是哪一段断了。",
            build: (at) => ({
              read: true,
              summary: "Cursor 额度用完了，等你决定是否换一个助手。",
              questions: [],
              tasks: [seedTask("区分断线位置的提示", "Corral/relay", "Cursor", "failed", at + 15 * min)],
              messages: [say("coordinator", "Cursor 这周的额度用完了，改动做到一半。要换成 Codex 接着做吗？", at + 16 * min)],
            }),
          },
        ],
      },
    ];
  }
  return [
    {
      offset: 0,
      items: [
        {
          sentence: "Scrolling the Corral session list on iPhone stutters when there are lots of sessions.",
          build: (at) => ({
            read: true,
            summary: "Reproducing the stutter on iPhone and measuring list rendering.",
            questions: [],
            tasks: [seedTask("Reproduce and fix list scrolling stutter", "Corral/ios", "Codex", "running", at + 2 * min)],
            messages: [say("coordinator", "Codex is reproducing this on iPhone with 300 sessions first, then looking at list rendering. I'll check the result when it's done.", at + 2 * min)],
          }),
        },
        {
          sentence: "Notely PDF export sometimes turns Chinese text into boxes. Fix it and verify with a real file.",
          build: (at) => ({
            read: false,
            summary: "Done and checked.",
            questions: [],
            tasks: [seedTask("Fix missing glyphs in PDF export", "Notely", "Claude Code", "done", at + 30 * min)],
            messages: [
              say("coordinator", "Claude Code is taking this in Notely. I'll check the result when it's done.", at + min),
              result(
                "Export now embeds a subset of the CJK font, so the missing glyphs are fixed.",
                "Exported 3 real mixed-language documents and checked every page: no boxes. All 18 export tests pass.",
                "Claude Code",
                "Notely",
                29,
                at + 31 * min,
              ),
            ],
          }),
        },
        {
          sentence: "Sometimes I get sent back to the sign-in page right after signing in",
          build: (at) => {
            const qid = newId("q");
            return {
              read: false,
              summary: "Confirming the project and what actually happens.",
              tasks: [],
              questions: [{ id: qid, state: "open", blocking: true }],
              messages: [say("coordinator", "Which project is this? Roughly how long after signing in does it happen, and is it every device or just one?", at + min, "question", qid)],
            };
          },
        },
      ],
    },
    {
      offset: -1,
      items: [
        {
          sentence: "Codex session titles in SessKit are sometimes empty. Find out why.",
          build: (at) => ({
            read: true,
            summary: "Done and checked.",
            questions: [],
            tasks: [seedTask("Find the cause of empty Codex titles", "SessKit", "Pi", "done", at + 40 * min)],
            messages: [
              say("coordinator", "Pi is looking into this in SessKit.", at + min),
              result("Newer Codex versions write the title to a different record; the reader now covers it.", "Replayed 12 real Codex histories: every title is correct. 3 new parser tests pass.", "Pi", "SessKit", 38, at + 41 * min),
            ],
          }),
        },
        {
          sentence: "Tidy up the Beacon README.",
          build: (at) => ({
            read: true,
            summary: "Stopped at your request.",
            questions: [],
            tasks: [seedTask("Tidy up README", "Beacon", "OpenCode", "canceled", at + 20 * min)],
            messages: thread(
              say("coordinator", "OpenCode is taking this in Beacon.", at + min),
              say("owner", "Let's not do this one for now.", at + 18 * min),
              say("coordinator", "Okay, I've stopped it.", at + 19 * min),
            ),
          }),
        },
      ],
    },
    {
      offset: -3,
      items: [
        {
          sentence: "The Grove home page loads too slowly. See if first paint can get under a second.",
          build: (at) => ({
            read: true,
            summary: "Done and checked.",
            questions: [],
            tasks: [seedTask("First-paint optimization", "Grove", "Codex", "done", at + 55 * min)],
            messages: [
              say("coordinator", "Codex is taking this in Grove.", at + min),
              result("Removed a large dependency from the first screen and prefetched the key data.", "Measured 5 times on throttled 4G: median first paint 0.8 s (was 2.6 s).", "Codex", "Grove", 52, at + 56 * min),
            ],
          }),
        },
        "Think about whether Ideas should support writing directly on the phone.",
      ],
    },
    {
      offset: -10,
      items: [
        {
          sentence: "Beacon feeds should retry automatically after a timeout.",
          build: (at) => ({
            read: true,
            summary: "Done and checked.",
            questions: [],
            tasks: [seedTask("Automatic retry after timeout", "Beacon", "Claude Code", "done", at + 25 * min)],
            messages: [result("Timeouts now retry three times with backoff before a feed is marked unavailable.", "Simulated 5 timeout cases; all retried as expected. 9 related tests pass.", "Claude Code", "Beacon", 24, at + 26 * min)],
          }),
        },
        {
          sentence: "When the Corral relay drops, the phone should say which hop disconnected.",
          build: (at) => ({
            read: true,
            summary: "Cursor ran out of quota; waiting for you to decide whether to switch assistants.",
            questions: [],
            tasks: [seedTask("Say which hop disconnected", "Corral/relay", "Cursor", "failed", at + 15 * min)],
            messages: [say("coordinator", "Cursor is out of quota for the week with the change half done. Should Codex pick it up?", at + 16 * min)],
          }),
        },
      ],
    },
  ];
}

export function seed(): Pick<State, "sections" | "threads"> {
  const now = new Date();
  const sections: DaySection[] = [];
  const threads: Record<string, Thread> = {};
  for (const d of seedDays()) {
    const day = d.offset === 0 ? dayKey(now) : shiftDay(now, d.offset);
    const base = d.offset === 0 ? Date.now() - 95 * 60_000 : new Date(`${day}T10:12:00`).getTime();
    let text = "";
    const spans = [];
    let i = 0;
    for (const item of d.items) {
      if (text) text += "\n\n";
      const sentence = typeof item === "string" ? item : item.sentence;
      const from = text.length;
      text += sentence;
      if (typeof item === "string") continue;
      const at = base + i * 17 * 60_000;
      i += 1;
      const { read, ...rest } = item.build(at);
      const id = newId("th");
      const last = rest.messages.reduce((m, x) => Math.max(m, x.at), at);
      threads[id] = { id, day, quote: sentence, createdAt: at, lastReadAt: read ? last : at, ...rest };
      spans.push({ threadId: id, from, to: from + sentence.length });
    }
    sections.push({ day, text: d.offset === 0 ? text + "\n\n" : text, spans });
  }
  return { sections, threads };
}
