import type { CodeMappingRow } from "./codeMappingTypes";

/**
 * The search rules the server applies (`mapping_browse.search_filter`),
 * for the client-side filter used when the list is not server-paginated.
 *
 * - `field:value` searches one field; `concept_id:` is exact.
 * - An all-digit search is a source code and matches only the source code
 *   value: Epic vocabularies are OIDs, so the digits would otherwise match
 *   every row of a code system (#2376).
 * - Anything else searches every field as a substring.
 *
 * A prefix that is not a known field is ordinary text, so `urn:oid:1.2.840`
 * searches as a whole.
 */
type Field = "source_code" | "description" | "vocabulary" | "concept" | "concept_code" | "concept_id" | "organization";

const FIELD_ALIASES: Record<string, Field> = {
  source_code: "source_code", code: "source_code", source_code_value: "source_code",
  description: "description",
  vocabulary: "vocabulary", system: "vocabulary", source_system: "vocabulary",
  concept: "concept", concept_name: "concept",
  concept_code: "concept_code",
  concept_id: "concept_id",
  organization: "organization", org: "organization",
};

export const SEARCH_PLACEHOLDER = "Search text, or a number for a source code; one field: code:6784, concept:myeloma, concept_id:3004061, system:loinc";

export function parseSearch(search: string): { field: Field | null; value: string } {
  const trimmed = search.trim();
  const colon = trimmed.indexOf(":");
  if (colon > 0) {
    const field = FIELD_ALIASES[trimmed.slice(0, colon).trim().toLowerCase()];
    const value = trimmed.slice(colon + 1).trim();
    if (field && value) return { field, value };
  }
  return { field: null, value: trimmed };
}

function fieldValues(row: CodeMappingRow, field: Field): (string | undefined | null)[] {
  switch (field) {
    case "source_code": return [row.source_code];
    case "description": return [row.source_code_description];
    case "vocabulary": return [row.source_vocabulary_id];
    case "concept": return [row.destination_concept_name];
    case "concept_code": return [row.destination_concept_code];
    case "concept_id": return [];
    case "organization": return [row.organization_name, row.organization_slug,
      row.source_system_context?.label, ...(row.source_system_context?.names || [])];
  }
}

export function rowMatchesSearch(row: CodeMappingRow, search: string): boolean {
  const { field, value } = parseSearch(search);
  if (!value) return true;
  // The server's guard: at most ten ASCII digits, below the integer column's range.
  if (field === "concept_id") return /^\d{1,10}$/.test(value) && Number(value) < 2 ** 31 && row.destination_concept_id === Number(value);
  const q = value.toLowerCase();
  const values = field ? fieldValues(row, field)
    : /^\d+$/.test(value) ? [row.source_code]
    : [row.source_code, row.source_vocabulary_id, row.source_code_description,
      row.organization_name, row.organization_slug,
      row.source_system_context?.label, ...(row.source_system_context?.names || []),
      row.destination_concept_name, row.destination_concept_code];
  return values.some((v) => (v || "").toLowerCase().includes(q));
}
