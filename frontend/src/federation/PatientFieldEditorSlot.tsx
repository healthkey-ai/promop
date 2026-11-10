import PatientFieldEditor from './PatientFieldEditor';
import { useEditorSession, type EditorConnection } from './editorSession';

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
  name,
  label,
  readOnly = false,
  labelIncludesUnit = false,
  compact = false,
}: PatientFieldEditorSlotProps) {
  const store = useEditorSession({ apiClient, apiBasePath });
  return (
    <PatientFieldEditor
      store={store}
      name={name}
      readOnly={readOnly}
      label={label}
      labelIncludesUnit={labelIncludesUnit}
      compact={compact}
    />
  );
}
