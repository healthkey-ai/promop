import type { SourceSystemContext } from "./SourceEvidencePanel";

/** Browse by facility, showing every code under one exact source system. */
export function sourceSystemManagementUrl(system: string): string {
  return `/code-mappings/facilities?${new URLSearchParams({ system }).toString()}`;
}

export function hospitalContextText(context?: SourceSystemContext): string {
  if (!context || context.kind === "none") return "Global / hospital unverified";
  if (context.kind === "verified") return `Verified: ${context.label}`;
  if (context.kind === "observed") return `Observed at: ${context.label}`;
  return `Multiple observed facilities (${context.name_count})`;
}

export function hospitalContextDetails(context?: SourceSystemContext): string {
  if (!context || context.kind === "none") return "No verified hospital attribution";
  if (context.kind === "verified") return "Verified PRomop organization";
  if (context.kind === "observed") return "High-confidence facility evidence; not a verified organization";
  const names = context.names.join(", ");
  return `${names}${context.name_count > context.names.length ? ", and others" : ""}. Observed facilities are not verified organizations.`;
}
