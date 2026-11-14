import { useMemo } from 'react';

import { PatientFormContext, PatientFormRenewContext } from './PatientFormContext';
import { patientFormStoreId } from './patientFormRuntime';
import TherapyLinesPanel from './TherapyLinesPanel';
import {
  editorBasePath,
  editorIdentity,
  useRenewableEditorSession,
  type EditorConnection,
} from './editorSession';

export interface TherapyLinesEditorProps extends EditorConnection {
  /** The host's write gate. Read-only still lists the recorded lines. */
  readOnly?: boolean;
  /** Called after a line is authored, retracted or the completeness flag is
   *  changed. The host must reload the record and invalidate whatever query
   *  rendered the row: therapy mutations bypass the store's save queue, so
   *  `store.load()` never reaches its `onUpdated` listeners. */
  onChanged?: () => void;
}

/**
 * The therapy-line editor for a host that shares this React tree.
 *
 * Same panel as the Treatment tab, same store, same writes — mounted by a
 * trial-details row that only needs to author lines. Unlike the standalone
 * `mountTherapyLines` widget there is no token and no second axios client, and
 * no module-level transport: the host's own authenticated client is passed
 * through to every catalog read and every write.
 */
export default function TherapyLinesEditor({
  apiClient,
  apiBasePath,
  connectionKey,
  readOnly = false,
  onChanged,
}: TherapyLinesEditorProps) {
  // Renewable: when the client starts answering for another patient, the
  // panel's Retry starts a fresh store instead of reloading the retired one.
  const { store, renew } = useRenewableEditorSession({ apiClient, apiBasePath, connectionKey });
  // A new connection is a new patient context: an open line dialog would still
  // hold the previous patient's episode. Keyed so React remounts it. A renewed
  // store is a new patient context too.
  const identity = `${editorIdentity({ apiClient, apiBasePath, connectionKey })}#${patientFormStoreId(store)}`;
  // Stable identity: the panel keys its catalog callbacks on it, and a new
  // object each render would re-run their effects on every render.
  const connection = useMemo(
    () => ({ client: apiClient, basePath: editorBasePath({ apiClient, apiBasePath }) }),
    [apiClient, apiBasePath],
  );
  return (
    // The dialog reads the store from context to hold the host listeners
    // while it authors a line outside the save queue.
    <PatientFormContext.Provider value={store}>
    <PatientFormRenewContext.Provider value={renew}>
      <TherapyLinesPanel
        key={identity}
        store={store}
        readOnly={readOnly}
        onChanged={onChanged}
        connection={connection}
      />
    </PatientFormRenewContext.Provider>
    </PatientFormContext.Provider>
  );
}
