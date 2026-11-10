import { useMemo } from 'react';

import TherapyLinesPanel from './TherapyLinesPanel';
import { editorBasePath, useEditorSession, type EditorConnection } from './editorSession';

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
  const store = useEditorSession({ apiClient, apiBasePath, connectionKey });
  // Stable identity: the panel keys its catalog callbacks on it, and a new
  // object each render would re-run their effects on every render.
  const connection = useMemo(
    () => ({ client: apiClient, basePath: editorBasePath({ apiClient, apiBasePath }) }),
    [apiClient, apiBasePath],
  );
  return (
    <TherapyLinesPanel
      store={store}
      readOnly={readOnly}
      onChanged={onChanged}
      connection={connection}
    />
  );
}
