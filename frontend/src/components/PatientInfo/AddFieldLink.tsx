import { useContext } from 'react';
import { Plus } from 'lucide-react';
import { Link } from 'react-router-dom';
import { FieldMappingLinksContext, addFieldHref, type AddFieldReturn } from './fieldMappingLinks';

/**
 * A patient tab's Add field action (#721), for whoever is shown the fields'
 * globes: staff and org admins outside patient mode, and nobody else.
 *
 * A link, not a dialog on the patient page: the field is created in Field
 * Concept Mapping, opened in create mode on this tab and, from the disease
 * tab, scoped to the record's disease. The page and tab to come back to
 * travel as router state, so the URL carries nothing about the patient.
 */
export default function AddFieldLink({ tab, disease, returnPath }: {
  tab: string;
  /** The record's Disease Category code, on the disease tab. */
  disease?: string;
  returnPath: string;
}) {
  if (!useContext(FieldMappingLinksContext)) return null;
  const addFieldReturn: AddFieldReturn = { path: returnPath, tab };
  return (
    <Link
      to={addFieldHref(tab, disease)}
      state={{ addFieldReturn }}
      title="Add a field to this tab in Field Concept Mapping"
      className="inline-flex shrink-0 items-center gap-1 rounded border border-border px-2.5 py-1.5 text-xs font-medium text-foreground hover:bg-muted"
    >
      <Plus size={14} aria-hidden="true" /> Add Field
    </Link>
  );
}
