import type { LabValueStatus } from "@/federation/types";

export function StatusDot({ status }: { status: LabValueStatus }) {
  const color =
    status === "in_range"
      ? "bg-success-700"
      : status === "unknown"
        ? "bg-muted-foreground"
        : "bg-warning-700";
  const label =
    status === "in_range"
      ? "in range"
      : status === "below"
        ? "below"
        : status === "above"
          ? "above"
          : "no range";
  return (
    <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
      <span className={`h-1.5 w-1.5 rounded-full ${color}`} aria-hidden="true" />
      {label}
    </span>
  );
}
