import { expect, test } from "@playwright/test";
import { attachmentSource, attachmentTokens, contentText, formatBytes, referencedAssets, splitsAttachment } from "../src/input/content";

test("asset links preserve exact Markdown positions and Unicode labels", () => {
  const image = attachmentSource({ id: "asset_01", name: "截图 [小屏]\\原件.png", image: true });
  const file = attachmentSource({ id: "asset_02", name: "response.json", image: false });
  const source = `前文 ${image}\n说明 ${file} 后文。`;
  expect(attachmentTokens(source)).toEqual([
    { id: "asset_01", from: 3, to: 3 + image.length, image: true, label: "截图 [小屏]\\原件.png" },
    { id: "asset_02", from: source.indexOf(file), to: source.indexOf(file) + file.length, image: false, label: "response.json" },
  ]);
  expect(contentText(source)).toBe("前文 \n说明  后文。");
  expect(referencedAssets(source)).toEqual(["asset_01", "asset_02"]);
});

test("code, escaped examples and unrelated links never become attachments", () => {
  const token = attachmentSource({ id: "asset_03", name: "sample.png", image: true });
  for (const source of [`\`${token}\``, `\`\`\`md\n${token}\n\`\`\``, `    ${token}`, token.replace("[", "\\["), "![remote](https://example.test/a.png)", "[local](file:///tmp/a.png)", "[bad](ideas-asset:../other)"]) {
    expect(attachmentTokens(source), source).toEqual([]);
  }
  expect(attachmentTokens(`> ${token}`).map((t) => t.id)).toEqual(["asset_03"]);
});

test("atomic boundaries never bisect an attachment token", () => {
  const token = attachmentSource({ id: "asset_04", name: "data.zip", image: false });
  const source = `a ${token} z`;
  expect(splitsAttachment(source, 2, 2 + token.length)).toBe(false);
  expect(splitsAttachment(source, 2, 3)).toBe(true);
  expect(splitsAttachment(source, 4, 4)).toBe(true);
  expect(splitsAttachment(source, 0, source.length)).toBe(false);
  expect(splitsAttachment(source, 0, 2)).toBe(false);
});

test("identities are bounded and filenames cannot inject a source URL", () => {
  expect(() => attachmentSource({ id: "../other", name: "data.zip", image: false })).toThrow("Invalid attachment identity");
  const source = attachmentSource({ id: "asset_05", name: "x](https://example.test)\n[other", image: false });
  expect(attachmentTokens(source)).toHaveLength(1);
  expect(attachmentTokens(source)[0].label).toBe("x](https://example.test) [other");
  expect(formatBytes(18)).toBe("18 B");
  expect(formatBytes(18 * 1024)).toBe("18 KB");
  expect(formatBytes(50 * 1024 ** 2)).toBe("50 MB");
});
