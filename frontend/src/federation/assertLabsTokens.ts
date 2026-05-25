export function assertLabsTokens() {
  if (import.meta.env.PROD) return;

  const root = getComputedStyle(document.documentElement);

  const required = [
    "--hk-labs-bg-primary",
    "--hk-labs-bg-secondary",
    "--hk-labs-text-primary",
    "--hk-labs-text-secondary",
    "--hk-labs-text-brand",
    "--hk-labs-border-secondary",
    "--hk-labs-brand-25",
    "--hk-labs-brand-50",
    "--hk-labs-brand-200",
    "--hk-labs-brand-700",
    "--hk-labs-radius",
  ];

  const missing = required.filter((t) => !root.getPropertyValue(t).trim());

  if (missing.length > 0) {
    console.warn("[labs-results-remote] Missing CSS tokens:", missing);
  }
}
