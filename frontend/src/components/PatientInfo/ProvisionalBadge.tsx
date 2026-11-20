import { useProvisionalMark } from './ProvisionalContext';

/** "Provisional 82%" after a field's label when its value is a proposal. */
export default function ProvisionalBadge({ name }: { name: string }) {
  const mark = useProvisionalMark(name);
  if (!mark) return null;
  const title = mark.patientMode
    ? 'Not yet confirmed. This value comes from a record that has not been fully checked. '
      + 'If it is wrong or right, you can enter it yourself.'
    : `From a code mapping that is not approved yet (${mark.confidence}% confidence). `
      + 'Edit the field to confirm it as approved data.';
  return (
    <span
      className="rounded bg-amber-100 px-1.5 py-0.5 text-xs font-medium text-amber-800"
      title={title}
      data-testid={`provisional-${name}`}
    >
      Provisional {mark.confidence}%
    </span>
  );
}
