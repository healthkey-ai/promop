export function assertLabsTokens() {
  if (import.meta.env.PROD) return;

  const root = getComputedStyle(document.documentElement);

  const required = [
    "--promop-bg-primary",
    "--promop-bg-secondary",
    "--promop-text-primary",
    "--promop-text-secondary",
    "--promop-text-brand",
    "--promop-border-secondary",
    "--promop-brand-25",
    "--promop-brand-50",
    "--promop-brand-200",
    "--promop-brand-700",
    "--promop-radius",
  ];

  const missing = required.filter((t) => !root.getPropertyValue(t).trim());

  if (missing.length > 0) {
    console.warn("[labs-results-remote] Missing CSS tokens:", missing);
  }
}
