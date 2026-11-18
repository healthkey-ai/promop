/** Make an embedded row's control borderless inside the host's cell.
 *
 * The host draws the cell outline (EXACT's "Your Value" box). PROMOP's control
 * brings its own border, so the row shows two. Tailwind utilities cannot remove
 * it reliably: this remote ships no stylesheet into the host, and the host's
 * own rules are unlayered, so they win over any layered utility we add.
 *
 * The rule is therefore injected at runtime, scoped to the embedded row, and
 * marked important. Focus keeps its ring and its border, so the keyboard cue is
 * not lost; only the resting state loses the second outline.
 */
const STYLE = `
[data-promop-embedded] :is(input:not([type=checkbox]):not([type=radio]), select, button[role="combobox"]):not(:focus-visible) {
  border-width: 0 !important;
  background: transparent !important;
  padding-inline: 0 !important;
  box-shadow: none !important;
}
`;

let installed = false;

export function installEmbeddedStyles(): void {
  if (installed || typeof document === 'undefined') return;
  installed = true;
  const style = document.createElement('style');
  style.setAttribute('data-promop-embedded-style', '');
  style.textContent = STYLE;
  document.head.append(style);
}
