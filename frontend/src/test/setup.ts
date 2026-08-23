import "@testing-library/jest-dom/vitest";

// jsdom implements neither the Pointer Capture API nor scrollIntoView, both of
// which Radix primitives call unconditionally while opening a popover. Without
// these an interaction test against any Radix Select/Dropdown dies with
// "target.hasPointerCapture is not a function" before the assertion runs.
if (typeof Element !== "undefined") {
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
  }
  if (!Element.prototype.setPointerCapture) {
    Element.prototype.setPointerCapture = () => {};
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = () => {};
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }
}

// Radix measures its trigger to size the popover; jsdom has no layout engine.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}
