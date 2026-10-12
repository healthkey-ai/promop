/* Put everything this remote injects into the host page inside one cascade
 * layer.
 *
 * `injectStyles` appends our compiled stylesheet to the *host's* `<head>`, and
 * our `@layer utilities` block merged into the host's layer of the same name —
 * later in source order, so an equal-specificity utility of ours won. A bare
 * `.hidden` from this bundle beat HealthTree ONE's `hidden lg:flex` header:
 * the logo, chat, bell and Donate button disappeared on every screen this
 * remote mounted on, and the mobile hamburger appeared in their place.
 *
 * Hosts declare the order before anything else:
 *
 *     @layer properties, theme, base, components, mf-remote, utilities;
 *
 * so `mf-remote` sits above the host's preflight — our own screens still style
 * themselves — and below the host's utilities, which the host's chrome is
 * built from. Our internal layer order survives as sub-layers of it, and rules
 * we ship unlayered stay the strongest thing we have.
 *
 * A host that declares no order gets `mf-remote` appended after its own
 * layers, which is where our rules already sit today: nothing regresses.
 *
 * `@property`, `@keyframes` and `@font-face` are hoisted back out. Layers do
 * not change how any of them register, and `@property` inside a layer is not
 * honoured in every browser.
 */

const HOISTED = /^@(?:property|(?:-webkit-)?keyframes|font-face|charset)\b/i;

export function layerRemoteCss(css: string, layer = "mf-remote"): string {
  const hoisted: string[] = [];
  const layered: string[] = [];

  for (const rule of topLevelRules(css)) {
    const start = rule.replace(/^(?:\s|\/\*[\s\S]*?\*\/)+/, "");
    (HOISTED.test(start) ? hoisted : layered).push(rule);
  }

  const body = layered.join("").trim();
  return [hoisted.join("").trim(), body && `@layer ${layer}{${body}}`]
    .filter(Boolean)
    .join("\n");
}

/** Split a stylesheet into its top-level rules: balanced `{}`, or a `;` at
 *  depth zero. Comments and quoted strings are skipped rather than scanned —
 *  an apostrophe in a comment ("the host's own preflight") would otherwise
 *  open a string that swallows the rest of the file. */
function topLevelRules(css: string): string[] {
  const rules: string[] = [];
  let depth = 0;
  let start = 0;
  let quote: string | null = null;

  for (let i = 0; i < css.length; i++) {
    const c = css[i];

    // A backslash escapes the next character in a selector as much as in a
    // string: `.data-\[selected\=\'true\'\]` is a real utility class in this
    // bundle, and reading its `\'` as an opening quote swallowed everything
    // after it (the @property rules stopped looking top-level).
    if (c === "\\") {
      i++;
      continue;
    }
    if (quote) {
      if (c === quote) quote = null;
      continue;
    }
    if (c === "/" && css[i + 1] === "*") {
      const end = css.indexOf("*/", i + 2);
      i = end === -1 ? css.length : end + 1;
      continue;
    }
    if (c === '"' || c === "'") {
      quote = c;
    } else if (c === "{") {
      depth++;
    } else if (c === "}") {
      if (--depth <= 0) {
        depth = 0;
        rules.push(css.slice(start, i + 1));
        start = i + 1;
      }
    } else if (c === ";" && depth === 0) {
      rules.push(css.slice(start, i + 1));
      start = i + 1;
    }
  }

  if (start < css.length) rules.push(css.slice(start));
  return rules;
}
