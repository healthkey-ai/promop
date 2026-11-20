import type { WearableSourceSystem } from "./codeMappingTypes";

/** Every system is the default (an empty value); choosing one limits the file or share to it. */
export default function WearableSourceSystemSelect({
  systems, value, onChange, disabled = false, id,
}: {
  systems: WearableSourceSystem[];
  value: string;
  onChange: (next: string) => void;
  disabled?: boolean;
  id?: string;
}) {
  if (systems.length === 0) return null;
  return (
    <select
      id={id}
      aria-label="Source code system"
      title="Limit to one source code system"
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-800"
    >
      <option value="">All source systems</option>
      {systems.map((system) => <option key={system.id} value={system.id}>{system.label}</option>)}
    </select>
  );
}
