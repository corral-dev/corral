// Local calendar-day grouping (requirement 19; only today open by default). Wording follows Corral's sidebar:
// Today, Yesterday, weekday within the last week, then the date.
import { locale, t } from "./i18n";


export function dayKey(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function parseDay(key: string): Date {
  const [y, m, d] = key.split("-").map(Number);
  return new Date(y, m - 1, d);
}

export function daysAgo(key: string, now: Date): number {
  const a = parseDay(key);
  const b = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((b.getTime() - a.getTime()) / 86400000);
}

export function shiftDay(now: Date, delta: number): string {
  const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() + delta);
  return dayKey(d);
}

const weekday = new Intl.DateTimeFormat(locale, { weekday: "long" });
const monthDay = new Intl.DateTimeFormat(locale, { month: "long", day: "numeric" });
const monthDayYear = new Intl.DateTimeFormat(locale, { year: "numeric", month: "long", day: "numeric" });

/** Primary label of a day section. */
export function dayLabel(key: string, now: Date): string {
  const ago = daysAgo(key, now);
  const date = parseDay(key);
  if (ago === 0) return t("today");
  if (ago === 1) return t("yesterday");
  if (ago > 1 && ago < 7) return weekday.format(date);
  return date.getFullYear() === now.getFullYear() ? monthDay.format(date) : monthDayYear.format(date);
}

/** Only today is open by default (owner, 2026-11-03). */
export function defaultOpen(key: string, now: Date): boolean {
  return daysAgo(key, now) === 0;
}
