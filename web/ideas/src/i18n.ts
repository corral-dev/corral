// Interface copy. English and Simplified Chinese; the browser language decides,
// `?lang=en|zh` overrides it.

const en = {
  appName: "Ideas",
  placeholder: "What's on your mind?",
  today: "Today",
  yesterday: "Yesterday",
  pickUp: "Pick up here",
  "status.waiting": "Waiting for you",
  "status.attention": "Needs attention",
  "status.working": "Working",
  "status.done": "Done",
  "status.withdrawn": "Withdrawn",
  "status.discussed": "Discussed",
  "task.blocked": "Waiting on another task",
  "task.queued": "Queued",
  "task.running": "Working",
  "task.review": "Checking the result",
  "task.needs_input": "Waiting for you",
  "task.stalled": "Assistant unavailable",
  "task.done": "Done",
  "task.failed": "Failed",
  "task.canceled": "Stopped",
  coordinator: "Coordinator",
  you: "You",
  now: "Now",
  tasks: "Tasks",
  result: "Result",
  checked: "How it was checked",
  openSession: "Open session",
  reply: "Reply…",
  answer: "Answer…",
  send: "Send",
  close: "Close",
  reading: "Reading",
  sendsIn: "Goes to the coordinator in {s} s",
  newResult: "New result",
  minutes: "{n} min",
  countWorking: "{n} working",
  countWaiting: "{n} waiting for you",
  countAttention: "{n} needs attention|{n} need attention",
  countDone: "{n} done",
  countItems: "{n} idea|{n} ideas",
  fold: "Collapse",
  unfold: "Expand",
  unread: "New",
  question: "Question",
  account: "Account",
  signOut: "Sign out",
} as const;

export type Key = keyof typeof en;

const zh: Record<Key, string> = {
  appName: "Ideas",
  placeholder: "想到什么，写下来",
  today: "今天",
  yesterday: "昨天",
  pickUp: "回来先看这里",
  "status.waiting": "等你回复",
  "status.attention": "需要处理",
  "status.working": "进行中",
  "status.done": "已完成",
  "status.withdrawn": "已撤回",
  "status.discussed": "已讨论",
  "task.blocked": "等前置任务",
  "task.queued": "排队中",
  "task.running": "进行中",
  "task.review": "核对结果中",
  "task.needs_input": "等你回复",
  "task.stalled": "助手暂不可用",
  "task.done": "已完成",
  "task.failed": "失败",
  "task.canceled": "已停止",
  coordinator: "协调员",
  you: "你",
  now: "当前",
  tasks: "任务",
  result: "结果",
  checked: "怎么验证的",
  openSession: "打开会话",
  reply: "回复…",
  answer: "回答…",
  send: "发送",
  close: "关闭",
  reading: "正在读",
  sendsIn: "{s} 秒后交给协调员",
  newResult: "新结果",
  minutes: "{n} 分钟",
  countWorking: "{n} 项进行中",
  countWaiting: "{n} 项等你回复",
  countAttention: "{n} 项需要处理",
  countDone: "{n} 项已完成",
  countItems: "{n} 条想法",
  fold: "收起",
  unfold: "展开",
  unread: "新",
  question: "提问",
  account: "账号",
  signOut: "退出登录",
};

function detect(): "en" | "zh" {
  const forced = new URLSearchParams(location.search).get("lang");
  if (forced === "en" || forced === "zh") return forced;
  const langs = navigator.languages?.length ? navigator.languages : [navigator.language];
  for (const l of langs) {
    const lower = l.toLowerCase();
    if (lower.startsWith("zh")) return "zh";
    if (lower.startsWith("en")) return "en";
  }
  return "en";
}

export const lang = detect();
export const locale = lang === "zh" ? "zh-CN" : "en-US";
const table: Record<Key, string> = lang === "zh" ? zh : en;

/** `one|other` strings pick a form by `n` (English only; Chinese has one form). */
export function t(key: Key, vars?: Record<string, string | number>): string {
  let s = table[key];
  if (s.includes("|")) {
    const [one, other] = s.split("|");
    s = vars?.n === 1 ? one : other;
  }
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replace(`{${k}}`, String(v));
  return s;
}

const rtf = new Intl.RelativeTimeFormat(locale, { numeric: "auto", style: "long" });

export function relativeTime(at: number, now: number): string {
  const sec = Math.round((at - now) / 1000);
  const abs = Math.abs(sec);
  if (abs < 45) return lang === "zh" ? "刚刚" : "just now";
  if (abs < 3600) return rtf.format(Math.round(sec / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(sec / 3600), "hour");
  return rtf.format(Math.round(sec / 86400), "day");
}
