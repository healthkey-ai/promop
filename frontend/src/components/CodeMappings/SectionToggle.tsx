/**
 * The heading of a mapping-dialog section that opens on demand: its name, a
 * chevron, and, while it is closed, a short marker of what it holds so a
 * curator can tell there is something inside without opening it.
 */
export default function SectionToggle({ title, open, onToggle, marker }: {
  title: string;
  open: boolean;
  onToggle: () => void;
  /** Shown only while closed; '' for a section that holds nothing. */
  marker?: string;
}) {
  return (
    <h3 className="text-sm font-semibold text-slate-900">
      <button type="button" aria-expanded={open} onClick={onToggle}
        className="flex w-full items-center gap-2 text-left hover:underline">
        <span aria-hidden="true" className="inline-block w-3 text-slate-500">{open ? "▾" : "▸"}</span>
        <span>{title}</span>
        {!open && marker && (
          <span data-testid="section-marker" className="rounded bg-blue-50 px-1.5 py-0.5 text-[11px] font-medium text-blue-700">{marker}</span>
        )}
      </button>
    </h3>
  );
}
