import { useMemo } from 'react';

import GenomicsFindingsModal from './GenomicsFindingsModal';
import { editorBasePath, useEditorSession, type EditorConnection } from './editorSession';

export interface GenomicsEditorProps extends EditorConnection {
  /** The host's write gate; the dialog still opens to show the findings. */
  readOnly?: boolean;
  /** The host unmounts the dialog from here; PROMOP draws its own shell. */
  onClose?: () => void;
  /** Called after a finding is added, changed or removed. Findings endpoints
   *  bypass the store's save queue, so the host must reload the record and
   *  invalidate the trial row itself. */
  onChanged?: () => void;
}

/**
 * The genomic-findings dialog for a host that shares this React tree.
 *
 * Same dialog, editor and write path as the Genomics tab and the standalone
 * `mountGenomics` widget — but the client and base come from the host, so the
 * findings endpoints cannot reach whichever widget registered a global
 * transport last.
 */
export default function GenomicsEditor({
  apiClient,
  apiBasePath,
  connectionKey,
  readOnly = false,
  onClose,
  onChanged,
}: GenomicsEditorProps) {
  const store = useEditorSession({ apiClient, apiBasePath, connectionKey });
  const base = editorBasePath({ apiClient, apiBasePath });
  // Stable identity: the editor stores this callback and compares it.
  const changed = useMemo(() => onChanged ?? (() => {}), [onChanged]);
  return (
    <GenomicsFindingsModal
      store={store}
      client={apiClient}
      apiBase={base}
      readOnly={readOnly}
      onClose={onClose}
      onChanged={changed}
    />
  );
}
