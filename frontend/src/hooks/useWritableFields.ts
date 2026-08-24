import { useEffect, useState } from 'react';
import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';

/**
 * What the server says a client may do with each PatientRecord field.
 *
 * PatientRecord is a derived read model with no writable clinical columns, so an
 * editor cannot patch it — it writes the underlying OMOP fact and derivation
 * follows. The concept, unit and target table for each field come from the server
 * rather than living here: concept ids are resolved from the vocabulary tables and
 * move with vocabulary releases, so a copy in TypeScript would drift silently and
 * start writing facts against stale concepts.
 */
export type FieldKind = 'editable' | 'selectable' | 'computed' | 'alias' | null;

export interface FieldDescriptor {
  kind: FieldKind;
  writable: boolean;
  reason?: string;
  /** editable */
  target?: 'measurement' | 'observation' | 'condition' | 'drug_exposure' | 'procedure' | 'person';
  /** profile: the key the persons endpoint expects, which is not always the
   *  column named by `person_field`. */
  payload_field?: string;
  person_field?: string;
  endpoint?: string;
  concept_id?: number;
  code?: string;
  vocabulary?: string;
  display?: string;
  value_kind?: 'number' | 'string' | 'boolean' | 'date' | 'datetime';
  unit?: string | null;
  unit_concept_id?: number | null;
  type_concept_id?: number;
  source_value?: string;
  attributed_from?: string;
  /** alias */
  canonical?: string;
  /** computed */
  inputs?: string[];
  /** selectable */
  qualifies?: string;
  /** profile: the curated choices the server resolves a concept from. Not the
   *  whole vocabulary — OMOP's Race holds 1,409 concepts, which is not the
   *  question a clinical form asks. */
  options?: Array<{ value: string; code?: string }>;
  /** Several answers at once, stored comma-joined. */
  multiple?: boolean;
  /** Set when the field is mapped but this caller may not edit this patient —
   *  read-only for who is asking, rather than read-only in principle. */
  read_only_for_caller?: boolean;
  /** authored: what to write instead, since no single fact backs this field. */
  authored_via?: {
    target?: string;
    endpoint?: string;
    steps?: string[];
    asserted_regimen_field?: string;
  };
  group?: string;
}

export type FieldDescriptors = Record<string, FieldDescriptor>;

/**
 * Columns the record keeps for itself, which no editor may send.
 *
 * They are serializer read-only, and `updated_at` / `derived_at` move on every
 * write by definition — so echoing back a copy captured before a write is read as
 * an attempted change and refuses the request. Kept here because both the
 * provider editor and the federation view filter against it, and a copy that
 * drifted would resurrect the bug in whichever one fell behind.
 */
export const LIFECYCLE: ReadonlySet<string> = new Set([
  'id', 'person', 'organization', 'created_at', 'updated_at',
  'derived_at', 'derivation_version', 'user_edited_fields',
]);

/** Cached per patient, because the answer is per patient.
 *
 *  The descriptor used to be deployment metadata — the same for everyone — which
 *  is true of *which fields are mapped* and false of *who may edit them*. An
 *  analyst has read-only access to every record, so a shared cache would show
 *  them typeable boxes whose every save is refused. Keyed by person so one
 *  patient's answer is never served for another's. */
const cached = new Map<string, FieldDescriptors>();
const inflight = new Map<string, Promise<FieldDescriptors>>();

const keyFor = (personId?: number | string) =>
  personId === undefined || personId === null ? '' : String(personId);

export function __resetWritableFieldsCache() {
  cached.clear();
  inflight.clear();
}

export function fetchWritableFields(
  personId?: number | string,
): Promise<FieldDescriptors> {
  const key = keyFor(personId);
  const hit = cached.get(key);
  if (hit) return Promise.resolve(hit);

  let pending = inflight.get(key);
  if (!pending) {
    pending = clinicalClient()
      .get(clinicalUrl('/v1/patient-records/writable-fields/'), {
        // Without a person the server answers for the deployment. With one it
        // answers for this caller and this patient, which is what a tab needs
        // before it decides whether to render a box.
        params: key ? { person_id: key } : undefined,
      })
      .then((res) => {
        const data = (res.data ?? {}) as FieldDescriptors;
        cached.set(key, data);
        return data;
      })
      .finally(() => {
        inflight.delete(key);
      });
    inflight.set(key, pending);
  }
  return pending;
}

export function useWritableFields(personId?: number | string) {
  const key = keyFor(personId);
  const [descriptors, setDescriptors] = useState<FieldDescriptors>(
    () => cached.get(key) ?? {},
  );
  const [loading, setLoading] = useState(!cached.has(key));
  const [error, setError] = useState(false);

  useEffect(() => {
    let alive = true;
    const ready = cached.get(key);
    if (ready) {
      Promise.resolve().then(() => {
        if (!alive) return;
        setDescriptors(ready);
        setLoading(false);
      });
      return;
    }
    fetchWritableFields(personId)
      .then((d) => {
        if (alive) setDescriptors(d);
      })
      .catch(() => {
        // An unreachable descriptor must not make fields look editable. Callers
        // treat an absent entry as "not writable", so failing closed is the
        // safe direction: the tab renders read-only rather than offering an
        // edit that would 405 on save.
        if (alive) setError(true);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [key, personId]);

  return { descriptors, loading, error };
}
