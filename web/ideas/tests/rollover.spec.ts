import { expect, test, type Page } from "@playwright/test";

const DAY = "2026-11-03";
const NEXT = "2026-11-04";
const KEY = "corral.ideas.preview.v1:midnight@example.test";
const BEFORE = new Date("2026-11-03T23:59:50+08:00");
const shortText = "A thought that crosses midnight.";
const longText = Array.from({ length: 100 }, (_, i) => `Line ${i + 1}: keep this thought and its position.`).join("\n\n");

async function openDraft(page: Page, text = shortText, extra: Record<string, unknown> = {}) {
  await page.clock.install({ time: BEFORE });
  await page.clock.pauseAt(new Date(BEFORE.getTime() + 1_000));
  await page.route("**/oauth2/userinfo", (route) => route.fulfill({ json: { email: "midnight@example.test" } }));
  await page.addInitScript(({ key, day, text, extra }) => {
    if (!localStorage.getItem(key)) localStorage.setItem(key, JSON.stringify({ sections: [{ day, text, spans: [] }], threads: {}, ...extra }));
  }, { key: KEY, day: DAY, text, extra });
  await page.goto("/index.html?lang=en&settle=3600");
  await expect(page.locator(".day .cm-content").first()).toBeVisible();
  await page.clock.runFor(400);
}

async function focusLine(page: Page, index = 0) {
  const lines = page.locator(`[data-day="${DAY}"] .cm-line`);
  if (index) {
    // CodeMirror virtualizes long documents; scroll first and let its native viewport update.
    await page.evaluate((y) => window.scrollTo(0, y), index * 60);
    await expect.poll(async () => {
      await page.clock.runFor(64);
      index = await lines.evaluateAll((els) => els.findIndex((el) => {
        const top = el.getBoundingClientRect().top;
        return top > 100 && top < 400;
      }));
      return index;
    }).toBeGreaterThanOrEqual(0);
  }
  await lines.nth(index).click({ position: { x: 35, y: 10 } });
  await page.clock.runFor(32);
}

async function recordAnchor(page: Page) {
  return page.evaluate((day) => {
    const section = document.querySelector(`[data-day="${day}"]`);
    const content = section?.querySelector(".cm-content");
    const selection = window.getSelection();
    if (!section || !content || !selection?.rangeCount) throw new Error("Missing active editor selection");
    const rect = selection.getRangeAt(0).getBoundingClientRect();
    Object.assign(window, { midnightProof: { section, content, active: document.activeElement, anchor: selection.anchorNode, focus: selection.focusNode } });
    return {
      top: content.getBoundingClientRect().top,
      left: content.getBoundingClientRect().left,
      height: section.getBoundingClientRect().height,
      caretTop: rect.top, caretLeft: rect.left,
      anchorOffset: selection.anchorOffset, focusOffset: selection.focusOffset,
      selected: selection.toString(), scrollY: window.scrollY,
    };
  }, DAY);
}

async function expectPreserved(page: Page, before: Awaited<ReturnType<typeof recordAnchor>>) {
  const after = await page.evaluate((day) => {
    const proof = (window as unknown as { midnightProof: { section: Element; content: Element; active: Element; anchor: Node; focus: Node } }).midnightProof;
    const section = document.querySelector(`[data-day="${day}"]`);
    const content = section?.querySelector(".cm-content");
    const selection = window.getSelection();
    if (!section || !content || !selection?.rangeCount) throw new Error("Editor was lost during rollover");
    const rect = selection.getRangeAt(0).getBoundingClientRect();
    return {
      top: content.getBoundingClientRect().top, left: content.getBoundingClientRect().left,
      height: section.getBoundingClientRect().height, caretTop: rect.top, caretLeft: rect.left,
      anchorOffset: selection.anchorOffset, focusOffset: selection.focusOffset, selected: selection.toString(),
      sameSection: section === proof.section, sameEditor: content === proof.content,
      sameFocus: document.activeElement === proof.active,
      sameAnchor: selection.anchorNode === proof.anchor, sameSelectionEnd: selection.focusNode === proof.focus,
      newBottom: document.querySelector(`[data-day="2026-11-04"]`)?.getBoundingClientRect().bottom,
      scrollY: window.scrollY, scrollHeight: document.scrollingElement?.scrollHeight,
      padding: document.querySelector<HTMLElement>(".page")?.style.paddingBottom,
    };
  }, DAY);
  expect(after.sameSection && after.sameEditor && after.sameFocus && after.sameAnchor && after.sameSelectionEnd).toBe(true);
  for (const key of ["top", "left", "height", "caretTop", "caretLeft"] as const) expect(Math.abs(after[key] - before[key]), `${key}: ${JSON.stringify({ before, after })}`).toBeLessThanOrEqual(1);
  expect(after.anchorOffset).toBe(before.anchorOffset);
  expect(after.focusOffset).toBe(before.focusOffset);
  expect(after.selected).toBe(before.selected);
  expect(after.newBottom).toBeDefined();
  expect(after.newBottom).toBeLessThanOrEqual(0);
  await expect(page.locator(`[data-day="${DAY}"] > button`)).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator(`[data-day="${DAY}"] .day-label`)).toHaveText("Yesterday");
}

for (const scenario of [{ name: "page top", text: shortText, line: 0 }, { name: "deep scroll", text: longText, line: 45 }]) {
  test(`midnight preserves the editor, caret and pixels at ${scenario.name}`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await openDraft(page, scenario.text);
    await focusLine(page, scenario.line);
    const before = await recordAnchor(page);
    if (!scenario.line) expect(before.scrollY).toBe(0);
    else expect(before.scrollY).toBeGreaterThan(100);
    await page.evaluate((day) => {
      const content = document.querySelector(`[data-day="${day}"] .cm-content`);
      if (!content) throw new Error("Missing editor for frame sampling");
      const tops: number[] = [];
      Object.assign(window, { midnightFrameTops: tops });
      const sample = () => {
        tops.push(content.getBoundingClientRect().top);
        if (tops.length < 700) requestAnimationFrame(sample);
      };
      requestAnimationFrame(sample);
    }, DAY);
    await page.clock.runFor(10_000);
    await expectPreserved(page, before);
    const tops = await page.evaluate(() => (window as unknown as { midnightFrameTops: number[] }).midnightFrameTops);
    expect(tops.length).toBeGreaterThan(10);
    expect(Math.max(...tops.map((top) => Math.abs(top - before.top)))).toBeLessThanOrEqual(1);
    await page.keyboard.insertText(" continued");
    await expect(page.locator(`[data-day="${DAY}"] .cm-content`)).toContainText("continued");
    await page.keyboard.press("ControlOrMeta+z");
    await expect(page.locator(`[data-day="${DAY}"] .cm-content`)).not.toContainText("continued");
    await page.keyboard.press("ControlOrMeta+Shift+z");
    await expect(page.locator(`[data-day="${DAY}"] .cm-content`)).toContainText("continued");
    await page.clock.runFor(32);
    // Only an explicit upward scroll reveals the fresh editor; it is ready for new input.
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
    await page.clock.runFor(64);
    await expect.poll(() => page.locator(`[data-day="${NEXT}"] .day-label`).evaluate((el) => {
      const rect = el.getBoundingClientRect();
      return rect.top >= 0 && rect.bottom <= window.innerHeight;
    })).toBe(true);
    await expect(page.locator(`[data-day="${NEXT}"] .day-label`)).toBeInViewport();
    await page.locator(`[data-day="${NEXT}"] .cm-content`).click();
    await page.keyboard.insertText("A new day's thought.");
    await page.clock.runFor(300);
    const saved = await page.evaluate((key) => JSON.parse(localStorage.getItem(key) ?? "null"), KEY);
    expect(saved.sections.find((s: { day: string }) => s.day === DAY).text).toContain("continued");
    expect(saved.sections.find((s: { day: string }) => s.day === NEXT).text).toBe("A new day's thought.");
    expect(saved.sections.find((s: { day: string }) => s.day === DAY).open).toBeUndefined();
    await page.reload();
    await expect(page.locator(`[data-day="${DAY}"] > button`)).toHaveAttribute("aria-expanded", "false");
    await expect(page.locator(`[data-day="${NEXT}"] .cm-content`)).toContainText("A new day's thought.");
    expect(errors).toEqual([]);
  });
}

test("a nonempty selection survives rollover and can still be replaced", async ({ page }) => {
  await openDraft(page);
  await focusLine(page);
  await page.keyboard.press("Home");
  await page.keyboard.press("Shift+ArrowRight");
  await page.keyboard.press("Shift+ArrowRight");
  const before = await recordAnchor(page);
  expect(before.selected.length).toBeGreaterThan(0);
  await page.clock.runFor(10_000);
  await expectPreserved(page, before);
  await page.keyboard.insertText("replacement");
  await expect(page.locator(`[data-day="${DAY}"] .cm-content`)).toContainText("replacement");
});

for (const event of ["focus", "pageshow", "visibilitychange"]) {
  test(`return through ${event} catches up without empty skipped days or duplicate sections`, async ({ page }) => {
    await openDraft(page);
    await focusLine(page);
    const before = await recordAnchor(page);
    await page.clock.setSystemTime(new Date("2026-11-04T09:00:00+08:00"));
    await page.evaluate((event) => (event === "visibilitychange" ? document : window).dispatchEvent(new Event(event)), event);
    await expectPreserved(page, before);
    await page.clock.setSystemTime(new Date("2026-11-07T09:00:00+08:00"));
    await page.evaluate((event) => (event === "visibilitychange" ? document : window).dispatchEvent(new Event(event)), event);
    await page.evaluate((event) => (event === "visibilitychange" ? document : window).dispatchEvent(new Event(event)), event);
    await expect(page.locator(".day")).toHaveCount(3);
    await expect(page.locator('[data-day="2026-11-07"]')).toHaveCount(1);
    expect(await page.locator('[data-day="2026-11-07"]').evaluate((el) => el.getBoundingClientRect().bottom)).toBeLessThanOrEqual(0);
    await expect(page.locator(`[data-day="${DAY}"] > button`)).toHaveAttribute("aria-expanded", "true");
    const finalTop = await page.locator(`[data-day="${DAY}"] .cm-content`).evaluate((el) => el.getBoundingClientRect().top);
    expect(Math.abs(finalTop - before.top)).toBeLessThanOrEqual(1);
  });
}

test("manual choices persist while temporary rollover expansion does not", async ({ page }) => {
  const previous = { day: "2026-11-02", text: "Earlier notes", spans: [], open: true };
  await openDraft(page, shortText, { sections: [{ day: DAY, text: shortText, spans: [], open: false }, previous] });
  await expect(page.locator(`[data-day="${DAY}"] > button`)).toHaveAttribute("aria-expanded", "false");
  await page.clock.runFor(10_000);
  await expect(page.locator(`[data-day="${DAY}"] > button`)).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator('[data-day="2026-11-02"] > button')).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator(`[data-day="${NEXT}"] > button`)).toHaveAttribute("aria-expanded", "true");
  await page.clock.runFor(300);
  await page.reload();
  await expect(page.locator(`[data-day="${DAY}"] > button`)).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator('[data-day="2026-11-02"] > button')).toHaveAttribute("aria-expanded", "true");
});

test("a forward clock change is also caught by the fallback timer", async ({ page }) => {
  await openDraft(page);
  await focusLine(page);
  const before = await recordAnchor(page);
  await page.clock.setSystemTime(new Date("2026-11-04T12:00:00+08:00"));
  await page.clock.runFor(30_000);
  await expectPreserved(page, before);
});

test("an open discussion, its draft and composer selection survive midnight", async ({ page }) => {
  const quote = "A request with a discussion.";
  const at = BEFORE.getTime() - 60_000;
  await openDraft(page, quote, {
    sections: [{ day: DAY, text: quote, spans: [{ threadId: "discussion", from: 0, to: quote.length }] }],
    threads: { discussion: {
      id: "discussion", day: DAY, quote, createdAt: at, summary: "", lastReadAt: at,
      messages: [{ id: "comment", author: "coordinator", at, kind: "text", text: "A comment awaiting your reply." }],
      tasks: [], questions: [],
    } },
  });
  await page.locator(".cm-taken").click();
  const composer = page.locator("textarea.composer-input");
  await composer.fill("A reply that is still being written.");
  await composer.evaluate((el) => {
    if (!(el instanceof HTMLTextAreaElement)) throw new Error("Missing discussion textarea");
    el.setSelectionRange(2, 8);
    Object.assign(window, { discussionInput: el });
  });
  const before = await composer.evaluate((el) => ({ top: el.getBoundingClientRect().top, left: el.getBoundingClientRect().left }));
  await page.clock.runFor(10_000);
  expect(await composer.evaluate((el) => {
    if (!(el instanceof HTMLTextAreaElement)) throw new Error("Missing discussion textarea");
    return {
      same: el === (window as unknown as { discussionInput: Element }).discussionInput,
      focused: el === document.activeElement, start: el.selectionStart, end: el.selectionEnd,
    };
  })).toEqual({ same: true, focused: true, start: 2, end: 8 });
  await expect(composer).toHaveValue("A reply that is still being written.");
  const after = await composer.evaluate((el) => ({ top: el.getBoundingClientRect().top, left: el.getBoundingClientRect().left }));
  expect(after).toEqual(before);
  expect(await page.locator(`[data-day="${NEXT}"]`).evaluate((el) => el.getBoundingClientRect().bottom)).toBeLessThanOrEqual(0);
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await expect(page.locator(`[data-day="${DAY}"] > button`)).toHaveAttribute("aria-expanded", "true");
});

test("composition keeps the same DOM editor and receives no synthetic end or blur", async ({ page }) => {
  await openDraft(page);
  await focusLine(page);
  await page.locator(`[data-day="${DAY}"] .cm-content`).evaluate((el) => {
    Object.assign(window, { compositionEnds: 0, editorBlurs: 0 });
    el.addEventListener("compositionend", () => (window as unknown as { compositionEnds: number }).compositionEnds++);
    el.addEventListener("blur", () => (window as unknown as { editorBlurs: number }).editorBlurs++);
    el.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true, data: "跨" }));
  });
  const before = await recordAnchor(page);
  await page.clock.runFor(10_000);
  await expectPreserved(page, before);
  expect(await page.evaluate(() => ({
    ends: (window as unknown as { compositionEnds: number }).compositionEnds,
    blurs: (window as unknown as { editorBlurs: number }).editorBlurs,
  }))).toEqual({ ends: 0, blurs: 0 });
  await page.locator(`[data-day="${DAY}"] .cm-content`).dispatchEvent("compositionend", { data: "跨天" });
  await page.keyboard.insertText("跨天");
  await expect(page.locator(`[data-day="${DAY}"] .cm-content`)).toContainText("跨天");
});
