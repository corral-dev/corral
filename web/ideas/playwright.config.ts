import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  outputDir: "./test-results",
  workers: 1,
  timeout: 30_000,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:47812",
    timezoneId: "Asia/Shanghai",
    locale: "en-US",
    reducedMotion: "reduce",
  },
  webServer: {
    command: "python3 -m http.server 47812 --bind 127.0.0.1",
    url: "http://127.0.0.1:47812/index.html",
    reuseExistingServer: false,
    stdout: "ignore",
    stderr: "pipe",
  },
  projects: [
    { name: "calendar", testMatch: "calendar.spec.ts" },
    { name: "chromium-desktop", testMatch: "rollover.spec.ts", use: { browserName: "chromium", viewport: { width: 1440, height: 900 }, colorScheme: "light" } },
    { name: "chromium-mobile", testMatch: "rollover.spec.ts", use: { browserName: "chromium", viewport: { width: 390, height: 844 }, colorScheme: "dark", isMobile: true, hasTouch: true } },
    { name: "webkit-desktop", testMatch: "rollover.spec.ts", use: { browserName: "webkit", viewport: { width: 1440, height: 900 }, colorScheme: "dark" } },
    { name: "webkit-mobile", testMatch: "rollover.spec.ts", use: { browserName: "webkit", viewport: { width: 390, height: 844 }, colorScheme: "light", isMobile: true, hasTouch: true } },
    { name: "firefox-desktop", testMatch: "rollover.spec.ts", use: { browserName: "firefox", viewport: { width: 1024, height: 768 }, colorScheme: "light" } },
  ],
});
