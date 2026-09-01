import { useId } from "react";
import type { ReactNode } from "react";

export const READ_ONLY_CLASS =
  "h-10 rounded-md border border-slate-200 bg-slate-100 px-3 font-mono text-sm font-normal text-slate-700";
export const INPUT_CLASS = "h-10 rounded-md border border-slate-300 px-3 text-sm font-normal text-slate-950";

/**
 * An accessible tooltip with content rendered by the application, rather than
 * relying on the browser's native title bubble (which is absent on touch and
 * unavailable in several embedded browsers).
 */
export function HelpTip({ tip }: { tip: string }) {
  const tooltipId = useId();
  return (
    <span className="group relative inline-flex">
      <button
        type="button"
        aria-label="Help"
        aria-describedby={tooltipId}
        className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-slate-300 text-[10px] leading-none text-slate-500 hover:border-slate-500 hover:text-slate-700 focus:outline-none focus:ring-2 focus:ring-slate-400"
      >
        ?
      </button>
      <span
        id={tooltipId}
        role="tooltip"
        className="pointer-events-none absolute left-0 top-full z-20 mt-1 w-72 rounded-md bg-slate-900 px-3 py-2 text-xs font-normal normal-case leading-relaxed tracking-normal text-white opacity-0 shadow-lg transition-opacity group-hover:opacity-100 group-focus-within:opacity-100"
      >
        {tip}
      </span>
    </span>
  );
}

/** A labelled control with visible help on hover or keyboard focus. */
export function Field({
  id,
  label,
  tip,
  className = "",
  children,
}: {
  id: string;
  label: string;
  tip: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={`grid gap-1 ${className}`}>
      <div className="flex items-center gap-1">
        <label htmlFor={id} className="text-sm font-medium text-slate-700">
          {label}
        </label>
        <HelpTip tip={tip} />
      </div>
      {children}
    </div>
  );
}

/** Derived from the resolved concept or from Domain; shown, never chosen. */
export function ReadOnlyField({
  id,
  label,
  tip,
  value,
  testId,
  fullWidth,
}: {
  id: string;
  label: string;
  tip: string;
  value: string;
  testId?: string;
  fullWidth?: boolean;
}) {
  return (
    <Field id={id} label={label} tip={tip}>
      <input
        id={id}
        data-testid={testId}
        value={value}
        readOnly
        title={tip}
        placeholder="—"
        className={`${READ_ONLY_CLASS}${fullWidth ? " w-full" : ""}`}
      />
    </Field>
  );
}
