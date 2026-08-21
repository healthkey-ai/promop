import { useEffect, useState } from 'react';
import api from '@/api/axios';

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
  target?: 'measurement' | 'observation';
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
}

export type FieldDescriptors = Record<string, FieldDescriptor>;

/** Module-level cache: the descriptor is deployment metadata, identical for every
 *  caller and every patient, so refetching it per tab mount is pure waste. */
let cached: FieldDescriptors | null = null;
let inflight: Promise<FieldDescriptors> | null = null;

export function __resetWritableFieldsCache() {
  cached = null;
  inflight = null;
}

export function fetchWritableFields(): Promise<FieldDescriptors> {
  if (cached) return Promise.resolve(cached);
  if (!inflight) {
    inflight = api
      .get('/v1/patient-records/writable-fields/')
      .then((res) => {
        cached = (res.data ?? {}) as FieldDescriptors;
        return cached;
      })
      .finally(() => {
        inflight = null;
      });
  }
  return inflight;
}

export function useWritableFields() {
  const [descriptors, setDescriptors] = useState<FieldDescriptors>(cached ?? {});
  const [loading, setLoading] = useState(!cached);
  const [error, setError] = useState(false);

  useEffect(() => {
    let alive = true;
    if (cached) {
      setDescriptors(cached);
      setLoading(false);
      return;
    }
    fetchWritableFields()
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
  }, []);

  return { descriptors, loading, error };
}
