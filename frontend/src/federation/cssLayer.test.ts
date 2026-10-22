import { describe, expect, it } from "vitest";

import { layerRemoteCss } from "./cssLayer";

describe("layerRemoteCss", () => {
  it("puts the sheet in the mf-remote layer so the host's utilities outrank ours", () => {
    const out = layerRemoteCss("@layer utilities{.hidden{display:none}}");
    expect(out).toBe("@layer mf-remote{@layer utilities{.hidden{display:none}}}");
  });

  it("keeps unlayered rules unlayered inside mf-remote", () => {
    const out = layerRemoteCss(".exact-root{color:#111}");
    expect(out).toBe("@layer mf-remote{.exact-root{color:#111}}");
  });

  it("hoists @property and @keyframes, which layers do not affect", () => {
    const out = layerRemoteCss(
      "@property --tw-rotate{syntax:'<angle>';inherits:false}@keyframes pulse{50%{opacity:.5}}@layer utilities{.p-4{padding:1rem}}",
    );
    expect(out.indexOf("@property")).toBeLessThan(out.indexOf("@layer mf-remote"));
    expect(out.indexOf("@keyframes")).toBeLessThan(out.indexOf("@layer mf-remote"));
    expect(out).toContain("@layer mf-remote{@layer utilities{.p-4{padding:1rem}}}");
  });

  it("does not treat an apostrophe in a comment as a string", () => {
    const out = layerRemoteCss("/* the host's own preflight */\n.a{color:red}\n.b{color:blue}");
    expect(out).toContain(".a{color:red}");
    expect(out).toContain(".b{color:blue}");
  });

  it("ignores braces and semicolons inside quoted values", () => {
    const out = layerRemoteCss('.a:after{content:"};"}.b{color:red}');
    expect(out).toBe('@layer mf-remote{.a:after{content:"};"}.b{color:red}}');
  });

  it("treats a backslash as an escape outside strings too", () => {
    // Real class from the compiled bundle. Read as an opening quote, its `\'`
    // swallowed the rest of the sheet and nothing else parsed as top level.
    const css = String.raw`.data-\[selected\=\'true\'\]\:bg-accent{color:red}@keyframes spin{to{rotate:360deg}}`;
    const out = layerRemoteCss(css);
    expect(out.indexOf("@keyframes")).toBeLessThan(out.indexOf("@layer mf-remote"));
    expect(out).toContain(String.raw`@layer mf-remote{.data-\[selected\=\'true\'\]\:bg-accent{color:red}}`);
  });

  it("keeps a bare at-statement with the rest of the sheet", () => {
    const out = layerRemoteCss("@layer theme, base;@layer theme{:root{--x:1}}");
    expect(out).toBe("@layer mf-remote{@layer theme, base;@layer theme{:root{--x:1}}}");
  });

  it("returns nothing but the hoisted rules when there is nothing to layer", () => {
    expect(layerRemoteCss("@keyframes spin{to{rotate:360deg}}")).toBe(
      "@keyframes spin{to{rotate:360deg}}",
    );
  });
});
