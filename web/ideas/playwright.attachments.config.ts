import { defineConfig } from "@playwright/test";
import { homedir } from "node:os";
import { join } from "node:path";

export default defineConfig({
  testDir: "./tests",
  outputDir: join(process.env.XDG_CONFIG_HOME ?? join(homedir(), ".config"), "corral/development/ideas-mixed-input/test-results"),
  workers: 1,
  timeout: 30_000,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:47818",
    timezoneId: "Asia/Shanghai",
    locale: "en-US",
    reducedMotion: "reduce",
  },
  webServer: {
    command: "python3 -m http.server 47818 --bind 127.0.0.1",
    url: "http://127.0.0.1:47818/index.html",
    reuseExistingServer: false,
    stdout: "pipe",
    stderr: "pipe",
  },
  projects: [
    { name: "content", testMatch: "mixed-content.spec.ts" },
    { name: "attachments-chromium", testMatch: /(?:attachments|asset-repository)\.spec\.ts/, use: { browserName: "chromium", viewport: { width: 1440, height: 900 }, colorScheme: "light" } },
    { name: "attachments-webkit", testMatch: /(?:attachments|asset-repository)\.spec\.ts/, use: { browserName: "webkit", viewport: { width: 1024, height: 768 }, colorScheme: "dark" } },
    { name: "attachments-firefox", testMatch: /(?:attachments|asset-repository)\.spec\.ts/, use: { browserName: "firefox", viewport: { width: 1024, height: 768 }, colorScheme: "light" } },
    { name: "attachments-mobile", testMatch: /attachments\.spec\.ts/, use: { browserName: "webkit", viewport: { width: 390, height: 844 }, colorScheme: "light", isMobile: true, hasTouch: true } },
  ],
});
