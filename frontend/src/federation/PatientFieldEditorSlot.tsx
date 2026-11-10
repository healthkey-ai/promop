import { useEffect, useMemo, useRef } from 'react';

import PatientFieldEditor from './PatientFieldEditor';
import { editorIdentity, useEditorSession, type EditorConnection } from './editorSession';

export interface PatientFieldEditorSlotProps extends EditorConnection {
  /** The name the trial row carries: a canonical descriptor key or one of its
   *  declared `bindings` (the trial catalog's own attribute spelling). */
  name: string;
  /** The trial's reviewed wording. PROMOP's generated label is a fallback. */
  label?: string;
  /** The host's write gate. A read-only field still shows the value. */
  readOnly?: boolean;
  /** The caller's label already carries this field's unit; do not append it. */
  labelIncludesUnit?: boolean;
  /** Narrow layout for a table cell. */
  compact?: boolean;
  /** Called after the server acknowledges a save. The record, its derived
   *  values and the trial verdict have all moved, so the host must invalidate
   *  its trial queries. Not called for a failed write. */
  onChanged?: () => void;
}

/**
 * One patient-value editor for one trial row, for a host that shares this
 * React tree.
 *
 * EXACT's `renderFieldEditor` returns a node per row and falls back to its own
 * editor when the host returns `null`. A host therefore has to decide whether
 * PROMOP can resolve the row BEFORE returning this node — use
 * `usePromopFieldBindings` for that; this component draws the "no reviewed
 * binding" notice only for a race after the descriptor changed.
 */
export default function PatientFieldEditorSlot({
  apiClient,
  apiBasePath,
  connectionKey,
  name,
  label,
  readOnly = false,
  labelIncludesUnit = false,
  compact = false,
  onChanged,
}: PatientFieldEditorSlotProps) {
  const store = useEditorSession({ apiClient, apiBasePath, connectionKey });
  // The store acknowledges a write only after the server answers; a hard
  // failure never reaches its `updated` listeners. `onUpdated` keeps the
  // host's callback until the queue drains, so a save that outlives this row
  // still invalidates.
  //
  // The subscription is per store, and the latest callback is read through a
  // ref. Keying it on `onChanged` would leave the old listener registered until
  // the queue drains (the store retires it then) and add a second one, so an
  // inline callback from the host would invalidate a batch twice.
  const changed = useRef(onChanged);
  useEffect(() => { changed.current = onChanged; }, [onChanged]);
  // One stable listener per store. StrictMode replays setup → cleanup → setup;
  // while a batch is pending the cleanup defers its removal, so a fresh
  // closure would be registered a second time and notify twice. A Set holds
  // this one reference once.
  const listener = useMemo(() => () => { changed.current?.(); }, []);
  useEffect(() => store.onUpdated(listener), [store, listener]);
  // A new connection is a new patient context. A dated draft or a chosen unit
  // belongs to the old one and must not be committed into the new store.
  const identity = editorIdentity({ apiClient, apiBasePath, connectionKey });
  return (
    <PatientFieldEditor
      key={identity}
      store={store}
      name={name}
      readOnly={readOnly}
      label={label}
      labelIncludesUnit={labelIncludesUnit}
      compact={compact}
    />
  );
}
