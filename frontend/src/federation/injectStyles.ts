import css from "./labs.css?inline";
import { layerRemoteCss } from "./cssLayer";

let injected = false;

export function injectStyles() {
  // Importing the editor barrel must stay safe outside a browser (SSR, Node
  // tooling, a test that imports the module without a DOM).
  if (injected || typeof document === 'undefined' || !document.head) return;

  const imports: string[] = [];
  const rest = css.replace(
    /@import\s+(?:url\([^)]+\)|"[^"]+"|'[^']+')\s*;?/g,
    (match) => { imports.push(match); return ""; },
  );

  if (imports.length > 0) {
    const fontStyle = document.createElement("style");
    fontStyle.setAttribute("data-mf", "labs-results-remote-fonts");
    fontStyle.textContent = imports.join("\n");
    document.head.appendChild(fontStyle);
  }

  const style = document.createElement("style");
  style.setAttribute("data-mf", "labs-results-remote");
  style.textContent = layerRemoteCss(rest);
  document.head.appendChild(style);
  // Set after the insertion: a failed append must not leave the module
  // believing the styles are present.
  injected = true;
}
