import { useEffect, useId, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import type { WearableSourceSystem } from "./codeMappingTypes";

const ALL = "All source systems";

/** What the closed control says: every system, the one or two chosen, or how many. */
function summary(systems: WearableSourceSystem[], value: string[]): string {
  const chosen = systems.filter((system) => value.includes(system.id));
  if (chosen.length === 0) return ALL;
  if (chosen.length <= 2) return chosen.map((system) => system.label).join(", ");
  return `${chosen.length} source systems`;
}

type Choice = {
  systems: WearableSourceSystem[];
  /** The ticked system ids; none is every system. */
  value: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
};

/**
 * The tick-boxes themselves: "All source systems", then one per system. Any
 * number can be ticked (#2795): a group reviewing the HL7 PHR codes wants
 * HealthKit and Health Connect beside them, not one system at a time. None
 * ticked is every system, which is the default and what "All source systems"
 * returns to. Ticking every box is the same thing and is reported as none
 * (#2799): a file or share limited to "all six" would leave out a system added
 * to the tab later.
 *
 * What `onChange` receives are system ids in the order `systems` lists them,
 * whatever order they were ticked in.
 *
 * A dialog lays these out in the page, inside its own labelled group: a list
 * that drops below a button is cut off by a dialog that scrolls.
 */
export function WearableSourceSystemBoxes({ systems, value, onChange, disabled = false }: Choice) {
  const toggle = (systemId: string) => {
    const next = systems.filter((system) => (system.id === systemId) !== value.includes(system.id)).map((system) => system.id);
    onChange(next.length === systems.length ? [] : next);
  };
  // Each row sets its own weight, so the list reads the same under a bold label.
  const row = "flex cursor-pointer items-center gap-2 whitespace-nowrap rounded px-2 py-1 text-xs text-slate-800 hover:bg-slate-100";
  return (
    <>
      <label className={`${row} font-medium`}>
        <input type="checkbox" checked={value.length === 0} disabled={disabled} onChange={() => onChange([])} />
        {ALL}
      </label>
      {systems.map((system) => (
        <label key={system.id} className={`${row} font-normal`}>
          <input type="checkbox" checked={value.includes(system.id)} disabled={disabled} onChange={() => toggle(system.id)} />
          {system.label}
        </label>
      ))}
    </>
  );
}

/**
 * The same choice in a toolbar: a button that says what is chosen and opens
 * the tick-boxes below it. The list closes on Escape, on a click elsewhere and
 * when focus moves on.
 *
 * With no systems it renders nothing, unless `pending`: then it holds its place
 * as a disabled "All source systems", which is what the file is until the
 * systems are known, so the row does not shift when they arrive.
 */
export default function WearableSourceSystemSelect({
  systems, value, onChange, disabled = false, pending = false,
}: Choice & { pending?: boolean }) {
  const [open, setOpen] = useState(false);
  // A list left open when the control is disabled (a download started) must
  // not come back by itself when it is enabled again.
  if (disabled && open) setOpen(false);
  const listId = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);

  // A click anywhere else closes the list, and so does Escape wherever the
  // focus is: a browser that does not focus a clicked button leaves it outside.
  useEffect(() => {
    if (!open) return undefined;
    const outside = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  if (systems.length === 0 && !pending) return null;
  const waiting = systems.length === 0;
  const shown = summary(systems, value);
  const expanded = open && !disabled && !waiting;

  return (
    <div
      ref={root}
      className="relative inline-block"
      // Tabbing out closes the list, as an outside click does. A blur that
      // names no next element is left alone: some browsers send one when a
      // box is clicked, and closing then would lose the click.
      onBlur={(event) => {
        const next = event.relatedTarget;
        if (next instanceof Node && !event.currentTarget.contains(next)) setOpen(false);
      }}
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
        type="button"
        aria-label={`Source code systems: ${shown}`}
        // A disclosure, not a menu: what opens is a group of tick-boxes.
        aria-expanded={expanded}
        aria-controls={expanded ? listId : undefined}
        title="Limit to the source code systems you tick"
        disabled={disabled || waiting}
        onClick={() => setOpen(!open)}
        className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-800 hover:bg-slate-100 focus:outline-2 focus:outline-slate-700 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {shown} <ChevronDown size={12} aria-hidden="true" />
      </button>
      {expanded && (
        <div
          id={listId}
          role="group"
          aria-label="Source code systems"
          className="absolute left-0 top-full z-20 mt-1 min-w-48 rounded-md border border-slate-300 bg-white p-1 shadow-lg"
        >
          <WearableSourceSystemBoxes systems={systems} value={value} onChange={onChange} disabled={disabled} />
        </div>
      )}
    </div>
  );
}
