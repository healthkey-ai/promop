interface Props {
  source: string | null;
  labName?: string | null;
  reportFilename?: string | null;
  timeAgo?: string;
}

export function DataSourceBadge({ source, labName, reportFilename, timeAgo }: Props) {
  const parts: string[] = [];

  if (source === "document_extraction") {
    parts.push("Document");
  } else if (source === "patient_self_report") {
    parts.push("Manual");
  } else if (source) {
    parts.push(source);
  }

  if (labName) parts.push(labName);
  if (reportFilename) parts.push(reportFilename);
  if (timeAgo) parts.push(timeAgo);

  if (parts.length === 0) return null;

  return (
    <span
      className="inline-flex items-center rounded-sm border border-border bg-muted px-2 py-0.5 text-caption text-muted-foreground"
      aria-label={`Source: ${parts.join(", ")}`}
    >
      {parts.join(" · ")}
    </span>
  );
}
