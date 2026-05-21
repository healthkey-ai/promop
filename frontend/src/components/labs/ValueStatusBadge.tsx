import type { LabValueStatus } from "@/federation/types";

const styles: Record<Exclude<LabValueStatus, "unknown">, string> = {
  in_range: "bg-success-50 text-success-700 border-success-200",
  below: "bg-warning-50 text-warning-700 border-warning-200",
  above: "bg-warning-50 text-warning-700 border-warning-200",
};

const labels: Record<Exclude<LabValueStatus, "unknown">, string> = {
  in_range: "Normal",
  below: "Below",
  above: "Above",
};

export function ValueStatusBadge({
  status,
  size = "sm",
}: {
  status: LabValueStatus;
  size?: "xs" | "sm";
}) {
  if (status === "unknown") return null;
  const sizeClass = size === "xs" ? "px-1.5 py-0.5 text-[10px]" : "px-2 py-0.5 text-[11px]";
  return (
    <span className={`inline-flex items-center rounded-sm border font-medium ${sizeClass} ${styles[status]}`}>
      {labels[status]}
    </span>
  );
}
