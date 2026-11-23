import { useEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import type { WearableSourceSystem } from "./codeMappingTypes";

/** What the closed control says: every system, the one or two chosen, or how many. */
function summary(systems: WearableSourceSystem[], value: string[]): string {
  const chosen = systems.filter((system) => value.includes(system.id));
  if (chosen.length === 0) return "All source systems";
  if (chosen.length <= 2) return chosen.map((system) => system.label).join(", ");
  return `${chosen.length} source systems`;
}

/**
 * Which source code systems a file or share holds. Any number can be ticked
 * (#2795): a group reviewing the HL7 PHR codes wants HealthKit and Health
 * Connect beside them, not one system at a time. None ticked is every system,
 * which is the default and what "All source systems" returns to.
 *
 * `value` and what `onChange` receives are system ids in the order `systems`
 * lists them, whatever order they were ticked in.
 *
 * In a toolbar it is a button that opens the list. `inline` lays the same
 * boxes out in the page instead, for a dialog: a list that drops below its
 * button is cut off by a dialog that scrolls.
 */
export default function WearableSourceSystemSelect({
  systems, value, onChange, disabled = false, id, inline = false,
}: {
  systems: WearableSourceSystem[];
  value: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
  id?: string;
  inline?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);

  // A click anywhere else closes the list, as it does for a native select.
  useEffect(() => {
    if (!open) return undefined;
    const outside = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, [open]);

  if (systems.length === 0) return null;
  const shown = summary(systems, value);
  const toggle = (systemId: string) => onChange(
    systems.filter((system) => (system.id === systemId) !== value.includes(system.id)).map((system) => system.id));
  const row = "flex cursor-pointer items-center gap-2 whitespace-nowrap rounded px-2 py-1 text-xs text-slate-800 hover:bg-slate-100";
  const boxes = (
    <>
      <label className={`${row} font-medium`}>
        <input type="checkbox" checked={value.length === 0} disabled={disabled} onChange={() => onChange([])} />
        All source systems
      </label>
      {systems.map((system) => (
        <label key={system.id} className={row}>
          <input type="checkbox" checked={value.includes(system.id)} disabled={disabled} onChange={() => toggle(system.id)} />
          {system.label}
        </label>
      ))}
    </>
  );

  if (inline) {
    return <div id={id} role="group" aria-label="Source code systems" className="flex flex-wrap gap-x-1">{boxes}</div>;
  }

  return (
    <div
      ref={root}
      className="relative inline-block"
      onKeyDown={(event) => {
        if (event.key !== "Escape" || !open) return;
        // Close the list only: whatever holds it may listen for Escape too.
        event.stopPropagation();
        setOpen(false);
        trigger.current?.focus();
      }}
    >
      <button
        ref={trigger}
        id={id}
        type="button"
        aria-label={`Source code systems: ${shown}`}
        aria-haspopup="true"
        aria-expanded={open && !disabled}
        title="Limit to the source code systems you tick"
        disabled={disabled}
        onClick={() => setOpen(!open)}
        className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-800 hover:bg-slate-100 focus:outline-2 focus:outline-slate-700 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {shown} <ChevronDown size={12} aria-hidden="true" />
      </button>
      {open && !disabled && (
        <div
          role="group"
          aria-label="Source code systems"
          className="absolute left-0 top-full z-20 mt-1 min-w-48 rounded-md border border-slate-300 bg-white p-1 shadow-lg"
        >
          {boxes}
        </div>
      )}
    </div>
  );
}
