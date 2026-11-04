import { expect, test } from "@playwright/test";
import { nextMidnight } from "../src/calendar";

for (const scenario of [
  { zone: "Asia/Shanghai", input: "2026-11-03T23:59:59.999+08:00", output: "2026-11-04T00:00:00+08:00", hours: 1 / 3_600_000 },
  { zone: "America/New_York", input: "2026-03-08T00:00:00-05:00", output: "2026-03-09T00:00:00-04:00", hours: 23 },
  { zone: "America/New_York", input: "2026-11-01T00:00:00-04:00", output: "2026-11-02T00:00:00-05:00", hours: 25 },
  { zone: "Asia/Shanghai", input: "2026-12-31T22:00:00+08:00", output: "2027-01-01T00:00:00+08:00", hours: 2 },
  { zone: "Asia/Shanghai", input: "2028-02-28T12:00:00+08:00", output: "2028-02-29T00:00:00+08:00", hours: 12 },
]) {
  test(`local midnight ${scenario.input} in ${scenario.zone}`, () => {
    const previous = process.env.TZ;
    try {
      process.env.TZ = scenario.zone;
      const now = new Date(scenario.input);
      const next = nextMidnight(now);
      expect(next.toISOString()).toBe(new Date(scenario.output).toISOString());
      expect(next.getTime() - now.getTime()).toBeCloseTo(scenario.hours * 3_600_000, 5);
    } finally {
      if (previous === undefined) delete process.env.TZ;
      else process.env.TZ = previous;
    }
  });
}
