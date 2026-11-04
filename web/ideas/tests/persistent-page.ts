import { test as base, webkit } from "@playwright/test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Playwright WebKit ephemeral sessions do not support IDB Blob writes (upstream #42795).
// Exercise a normal isolated profile rather than pretending private-mode writes work.
export const test = base.extend({
  page: async ({ browser, browserName, contextOptions, baseURL, viewport, colorScheme, isMobile, hasTouch, locale, timezoneId, reducedMotion }, use, testInfo) => {
    const profile = browserName === "webkit" ? await mkdtemp(join(tmpdir(), "ideas-assets-")) : undefined;
    const options = { ...contextOptions, baseURL, viewport, colorScheme, isMobile, hasTouch, locale, timezoneId, reducedMotion, acceptDownloads: true,
      recordVideo: !isMobile && testInfo.title.includes("drag") ? { dir: testInfo.outputPath("motion"), size: viewport ?? { width: 1280, height: 900 } } : undefined,
    };
    try {
      const context = profile ? await webkit.launchPersistentContext(profile, { ...options, headless: true }) : await browser.newContext(options);
      const page = await context.newPage(); const video = page.video();
      try { await use(page); }
      finally {
        await context.close();
        if (video) await testInfo.attach("native-drag-motion", { path: await video.path(), contentType: "video/webm" });
      }
    } finally { if (profile) await rm(profile, { recursive: true, force: true }); }
  },
});
