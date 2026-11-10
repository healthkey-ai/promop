import TherapyLinesPanel from './TherapyLinesPanel';
import { useEditorSession, type EditorConnection } from './editorSession';

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
 * `mountTherapyLines` widget there is no token and no second axios client: the
 * host's own authenticated client does the talking.
 */
export default function TherapyLinesEditor({
  apiClient,
  apiBasePath,
  readOnly = false,
  onChanged,
}: TherapyLinesEditorProps) {
  const store = useEditorSession({ apiClient, apiBasePath });
  return <TherapyLinesPanel store={store} readOnly={readOnly} onChanged={onChanged} />;
}
