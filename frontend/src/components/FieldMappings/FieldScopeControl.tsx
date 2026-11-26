import { useEffect, useId, useRef } from "react";
import { HelpTip } from "@/components/UI/MappingFormPrimitives";
import type { DiseaseCategoryOption, FieldScope } from "@/types/fieldScope";

const TIP =
  "Base record: every patient has this field. Disease-specific: only patients in the ticked "
  + "Disease Categories have it. Changing the scope changes no stored value and no mapping.";

interface Props {
  scope: FieldScope;
  /** The Disease Category codes ticked; kept while the scope is base. */
  diseases: string[];
  options: DiseaseCategoryOption[];
  /** The categories could not be loaded, so `options` are only those already in use. */
  optionsIncomplete?: boolean;
  onChange: (scope: FieldScope, diseases: string[]) => void;
  /** A refusal for this control: the server's 400 under `diseases`, or the dialog's own. */
  error?: string;
  disabled?: boolean;
  /** Wording for what is scoped, when it is not a field (an answer, #1225). */
  words?: { legend: string; base: string; disease: string; tip: string };
}

/**
 * Whose record a field is part of (#2332): the base record every patient
 * has, or the extension of one or more Disease Categories. A membership edit
 * and nothing else: it changes no stored value and no mapping.
 */
export function FieldScopeControl({
  scope, diseases, options, optionsIncomplete = false, onChange, error, disabled, words,
}: Props) {
  const id = useId();
  const errorId = `${id}-error`;
  const fieldset = useRef<HTMLFieldSetElement>(null);
  // The control sits in a scrolling dialog body, below where Save is pressed.
  useEffect(() => {
    if (error) fieldset.current?.scrollIntoView?.({ block: "nearest" });
  }, [error]);

  // A category the field already names stays listed even when the categories
  // did not load, so saving cannot drop it unseen.
  const listed = new Set(options.map((option) => option.value));
  const all = [
    ...options,
    ...diseases.filter((code) => !listed.has(code)).map((code) => ({ value: code, label: code })),
  ];
  const toggle = (code: string) => onChange(
    "disease",
    diseases.includes(code) ? diseases.filter((d) => d !== code) : [...diseases, code],
  );

  return (
    <fieldset
      ref={fieldset}
      className="mb-4 rounded-md border border-slate-200 p-4"
      aria-describedby={error ? errorId : undefined}
    >
      <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
        {words?.legend ?? "Scope — whose record this field is part of"}
      </legend>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm text-slate-700">
        <label className="inline-flex items-center gap-2">
          <input
            type="radio"
            name={id}
            checked={scope === "base"}
            disabled={disabled}
            onChange={() => onChange("base", diseases)}
          />
          {words?.base ?? "Base record (every patient)"}
        </label>
        <label className="inline-flex items-center gap-2">
          <input
            type="radio"
            name={id}
            checked={scope === "disease"}
            disabled={disabled}
            onChange={() => onChange("disease", diseases)}
          />
          {words?.disease ?? "Disease-specific"}
        </label>
        <HelpTip tip={words?.tip ?? TIP} />
      </div>
      {scope === "disease" && (
        <div role="group" aria-label="Disease Categories" className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-sm">
          {all.length === 0 && !optionsIncomplete && (
            <span className="text-xs text-slate-500">No Disease Categories are defined yet.</span>
          )}
          {all.map((option) => (
            <label key={option.value} className="inline-flex items-center gap-2 text-slate-700">
              <input
                type="checkbox"
                checked={diseases.includes(option.value)}
                disabled={disabled}
                onChange={() => toggle(option.value)}
              />
              <span className="font-mono text-xs">{option.value}</span>
              {option.label !== option.value && <span className="text-xs text-slate-500">{option.label}</span>}
            </label>
          ))}
        </div>
      )}
      {scope === "disease" && optionsIncomplete && (
        <p className="mt-2 text-xs text-amber-700">
          The Disease Categories could not be loaded, so only those already in use are listed. Reload the page to try again.
        </p>
      )}
      {error && (
        <p id={errorId} role="alert" className="mt-2 text-sm text-red-600">
          {error}
        </p>
      )}
    </fieldset>
  );
}
