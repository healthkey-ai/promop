/** Format a date for display.
 *
 * A date-only value (`YYYY-MM-DD`) is a calendar day, not an instant. Parsing it
 * as UTC midnight and formatting it in the browser's zone prints the previous
 * day west of UTC, so date-only values are built from their parts instead.
 * Values with a time component keep the timestamp behaviour.
 */
export function formatDate(dateStr: string | null): string {
  if (!dateStr) return "";
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr);
  try {
    const value = dateOnly
      ? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
      : new Date(dateStr);
    return value.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return dateStr;
  }
}
