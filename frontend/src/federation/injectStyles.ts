import css from "./labs.css?inline";

let injected = false;

export function injectStyles() {
  if (injected) return;
  injected = true;

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
  style.textContent = rest;
  document.head.appendChild(style);
}
