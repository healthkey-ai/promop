import { Fragment } from "react";
import type { PatientSourceCode, WearableSourceCode } from "../../types/sourceCodes";
import { SourceCodeReadingsRow } from "./OmopReadings";

const DASH = "—";

const BADGE = "inline-flex whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-medium";
// The colours the rest of the app uses: green for approved data (this tab's
// Approved section), amber for a proposal (ProvisionalBadge).
const ACCEPTED = `${BADGE} bg-green-100 text-green-800`;
const PROVISIONAL = `${BADGE} bg-amber-100 text-amber-800`;
const NEUTRAL = `${BADGE} bg-slate-100 text-slate-600`;

const ACCEPTED_TITLE = "Stored at an approved mapping's concept.";
const PROVISIONAL_TITLE = "Given a concept by a proposed mapping. Accepted once the mapping is approved.";
const UNRESOLVED_TITLE = "Stored and waiting: no mapping gives these rows a concept yet.";

const TABLE_LABELS: Record<string, string> = {
  measurement: "Measurement",
  observation: "Observation",
};

const MAPPING_NOTES: Record<string, string> = {
  approved: "mapping approved",
  proposed: "mapping proposed",
  unmapped: "no mapping",
};

/** The code as the device sends it. */
const codeOf = (code: PatientSourceCode) => code.source_code || code.source_value;

/**
 * Whether the code's rows are of more than one kind. The server says "mixed"
 * then; the counts are read as well, so a status this page does not know
 * still shows what is stored.
 */
function isMixed({ wearable }: WearableSourceCode): boolean {
  const kinds = [wearable.accepted_rows, wearable.provisional_rows, wearable.unresolved_rows]
    .filter((count) => count > 0).length;
  return wearable.status === "mixed" || kinds > 1;
}

/** The one kind every row is, by the counts: what the status says, read from the rows. */
function singleKind({ wearable }: WearableSourceCode): "accepted" | "provisional" | "unresolved" | "none" {
  if (wearable.accepted_rows > 0) return "accepted";
  if (wearable.provisional_rows > 0) return "provisional";
  if (wearable.unresolved_rows > 0) return "unresolved";
  return "none";
}

/**
 * The mapping's own status, where it says something the badge does not: rows
 * are accepted but the mapping is back to proposed, rows are unresolved
 * because the code has no mapping, or an approved mapping has not been
 * applied to them yet.
 */
function mappingNote(code: WearableSourceCode): string | null {
  const note = MAPPING_NOTES[code.mapping_status] ?? `mapping ${code.mapping_status}`;
  if (isMixed(code)) return note;
  if (code.wearable.status === "accepted" && code.mapping_status === "approved") return null;
  if (code.wearable.status === "provisional" && code.mapping_status === "proposed") return null;
  return note;
}

function dates({ wearable }: WearableSourceCode): string {
  const first = wearable.first_date ?? wearable.last_date;
  const last = wearable.last_date ?? wearable.first_date;
  if (!first || !last) return DASH;
  return first === last ? first : `${first} to ${last}`;
}

function StatusBadges({ code }: { code: WearableSourceCode }) {
  const { wearable } = code;
  const percent = wearable.confidence != null ? `${wearable.confidence}%` : "";
  if (isMixed(code)) {
    return (
      <>
        {wearable.accepted_rows > 0 && (
          <span className={ACCEPTED} title={ACCEPTED_TITLE}>Accepted {wearable.accepted_rows}</span>
        )}
        {wearable.provisional_rows > 0 && (
          <span
            className={PROVISIONAL}
            title={percent ? `${PROVISIONAL_TITLE} Lowest confidence ${percent}.` : PROVISIONAL_TITLE}
          >
            Provisional {wearable.provisional_rows}
          </span>
        )}
        {wearable.unresolved_rows > 0 && (
          <span className={NEUTRAL} title={UNRESOLVED_TITLE}>Unresolved {wearable.unresolved_rows}</span>
        )}
      </>
    );
  }
  switch (singleKind(code)) {
    case "accepted":
      return <span className={ACCEPTED} title={ACCEPTED_TITLE}>Accepted</span>;
    case "provisional":
      return (
        <span className={PROVISIONAL} title={PROVISIONAL_TITLE}>
          Provisional{percent ? ` ${percent}` : ""}
        </span>
      );
    case "unresolved":
      return <span className={NEUTRAL} title={UNRESOLVED_TITLE}>Unresolved</span>;
    default:
      return <span className={NEUTRAL} title="No OMOP rows are stored for this code.">No rows</span>;
  }
}

/**
 * The Wearables section at the top of a patient's Source Codes tab (#2835):
 * one row per device code, with the concept it is mapped to and whether its
 * stored rows are accepted, provisional or unresolved. A row opens the same
 * Edit Mapping dialog as any other row of the tab. Renders nothing for a
 * patient with no device codes.
 */
export default function PatientWearableCodes({
  codes,
  onRowClick,
  personId,
}: {
  codes: WearableSourceCode[];
  /** The patient, for reading a code's stored rows. Absent: no arrow. */
  personId?: string;
  /** The tab's own row handler. Absent for a reader: the row opens nothing (#2416). */
  onRowClick?: (code: PatientSourceCode) => void;
}) {
  if (codes.length === 0) return null;

  return (
    <section data-testid="wearable-codes">
      <div className="flex items-center gap-2 px-2 py-1.5 text-sm font-medium">
        <h3>Wearables</h3>
        <span className="text-muted-foreground">({codes.length})</span>
      </div>
      <p className="mb-2 px-2 text-xs text-muted-foreground">
        Device codes and the OMOP concept each is mapped to. Accepted rows come from an approved
        mapping. Provisional rows come from a proposed mapping and become accepted when it is
        approved. Unresolved rows are stored and wait for a mapping.
      </p>

      <div className="overflow-x-auto rounded-md border">
        <table className="w-full text-sm" aria-label="Wearable codes">
          <thead className="border-b bg-muted/50">
            <tr>
              <th scope="col" className="px-3 py-2 text-left font-medium">Source</th>
              <th scope="col" className="px-3 py-2 text-left font-medium">OMOP concept</th>
              <th scope="col" className="px-3 py-2 text-left font-medium">Table</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Rows</th>
              <th scope="col" className="px-3 py-2 text-left font-medium">Dates</th>
              <th scope="col" className="px-3 py-2 text-left font-medium">Status</th>
              <th scope="col" className="px-3 py-2 text-left font-medium">30-day metrics</th>
            </tr>
          </thead>
          <tbody>
            {codes.map((code, i) => {
              const { wearable } = code;
              const id = `${wearable.device}-${codeOf(code)}`;
              const note = mappingNote(code);
              const fields = (wearable.fields ?? []).map((field) => field.label);
              const readings = personId != null && wearable.rows > 0;
              return (
                <Fragment key={`${code.omop_table}-${id}-${i}`}>
                <tr
                  data-testid={`wearable-code-${id}`}
                  className={[
                    onRowClick ? "cursor-pointer align-top hover:bg-muted/30" : "align-top",
                    readings ? "" : "border-b",
                  ].join(" ")}
                  onClick={onRowClick ? () => onRowClick(code) : undefined}
                >
                  <td className="px-3 py-2">
                    <div className="text-xs text-muted-foreground">{wearable.source}</div>
                    <div className="font-mono text-xs">{codeOf(code)}</div>
                    {code.source_code_description && (
                      <div className="text-xs">{code.source_code_description}</div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-xs">
                    {code.mapping_destination_concept_code
                      ? (
                        <>
                          <div className="whitespace-nowrap font-mono text-xs">
                            {[code.mapping_destination_vocabulary_id, code.mapping_destination_concept_code]
                              .filter(Boolean).join(" ")}
                          </div>
                          <div>{code.mapping_target_concept_name || DASH}</div>
                        </>
                      )
                      : <span className="text-muted-foreground">{DASH}</span>}
                  </td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {code.omop_table ? (TABLE_LABELS[code.omop_table] ?? code.omop_table) : DASH}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">{wearable.rows}</td>
                  <td className="whitespace-nowrap px-3 py-2 text-xs">{dates(code)}</td>
                  <td className="px-3 py-2" data-testid={`wearable-status-${id}`}>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <StatusBadges code={code} />
                    </div>
                    {note && <div className="mt-1 text-[11px] text-muted-foreground">{note}</div>}
                  </td>
                  <td className="px-3 py-2 text-xs">{fields.length ? fields.join(", ") : DASH}</td>
                </tr>
                {readings && (
                  <SourceCodeReadingsRow
                    personId={personId}
                    table={code.omop_table}
                    code={code.source_value}
                    vocabulary={code.source_vocabulary_id ?? ""}
                    count={wearable.rows}
                    colSpan={7}
                    testId={`wearable-readings-${wearable.device}-${codeOf(code)}`}
                  />
                )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
