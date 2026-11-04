// Calendar boundaries, not fixed 24-hour intervals (including DST and page suspension).
export function nextMidnight(now: Date): Date {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
}

export function watchCalendar(refresh: (now: Date) => void): () => void {
  let midnightTimer = 0;
  const update = () => {
    const now = new Date();
    refresh(now);
    clearTimeout(midnightTimer);
    midnightTimer = window.setTimeout(update, Math.max(1, nextMidnight(now).getTime() - now.getTime()));
  };
  const visible = () => {
    if (document.visibilityState === "visible") update();
  };

  update();
  // Also catches clock/time-zone changes while the page stays in the foreground.
  const clockTimer = window.setInterval(update, 30_000);
  window.addEventListener("focus", update);
  window.addEventListener("pageshow", update);
  document.addEventListener("visibilitychange", visible);
  return () => {
    clearTimeout(midnightTimer);
    clearInterval(clockTimer);
    window.removeEventListener("focus", update);
    window.removeEventListener("pageshow", update);
    document.removeEventListener("visibilitychange", visible);
  };
}
