// What the OMOP tab and the readings under it share: the payload's shapes, the
// order of the tables, and how a moment is shown (#2863).

export const DASH = "—";

/** One stored OMOP row, as the group and wearable-code row endpoints return it. */
export interface Reading {
  id: number;
  date: string;
  /** Null for a row that stores only a day, as every wearable row does. */
  datetime: string | null;
  value_as_number: number | null;
  value_as_string: string | null;
  unit: string | null;
  source_value: string | null;
  concept_id: number;
  status: "accepted" | "provisional" | "unresolved";
  confidence: number | null;
  is_erroneous: boolean;
}

/** A measurement or observation concept (or, at concept 0, a source value) and what it holds. */
export interface OmopGroup {
  concept_id: number;
  concept_name?: string | null;
  concept_code?: string | null;
  vocabulary_id?: string | null;
  /** Set on an unresolved group: the code its rows were stored under. */
  source_value: string | null;
  count: number;
  provisional_rows: number;
  rows_with_time: number;
  /** The group is a device's: the importer's rows under a code the patient's inventory holds as one. */
  wearable: boolean;
  wearable_rows: number;
  latest_date: string;
  latest_datetime: string | null;
  earliest_date: string;
  earliest_datetime: string | null;
}

export interface OmopTable {
  key: string;
  label: string;
  count: number;
  rows: Array<Record<string, unknown>>;
  /** Measurements and observations: grouped by concept, their rows read per group. */
  grouped?: boolean;
  groups?: OmopGroup[];
  unresolved_groups?: OmopGroup[];
  truncated?: boolean;
}

// Person first, then the two tables the readings are in; the rest as the server lists them.
const TABLE_ORDER = ["person", "measurements", "observations"];
/** The tables that open with the tab; every other table starts closed. */
export const OPEN_BY_DEFAULT = new Set(TABLE_ORDER);

export function orderTables(tables: OmopTable[]): OmopTable[] {
  const rank = (key: string) => {
    const at = TABLE_ORDER.indexOf(key);
    return at < 0 ? TABLE_ORDER.length : at;
  };
  // Array.sort is stable: tables outside the list keep the server's order.
  return [...tables].sort((a, b) => rank(a.key) - rank(b.key));
}

export const WEARABLE_LABEL = "From a wearable device";

export const DATE_ONLY_TITLE = "Only a date is stored for this reading; no time of day was recorded.";

/**
 * A moment as the server holds it, in UTC, or the bare day when the row has
 * no time. A time of day is never made up for a day-only row.
 */
export function formatMoment(datetime: string | null | undefined, date: string | null | undefined) {
  if (datetime) {
    const parsed = new Date(datetime);
    if (!Number.isNaN(parsed.getTime())) {
      return { text: `${parsed.toISOString().slice(0, 16).replace("T", " ")} UTC`, dateOnly: false };
    }
  }
  return { text: date ?? DASH, dateOnly: Boolean(date) };
}
