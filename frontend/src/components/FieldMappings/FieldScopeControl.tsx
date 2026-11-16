import type { DiseaseCategoryOption, FieldScope } from "@/types/fieldScope";

interface Props {
  scope: FieldScope;
  /** The Disease Category codes ticked; kept while the scope is base. */
  diseases: string[];
  options: DiseaseCategoryOption[];
  onChange: (scope: FieldScope, diseases: string[]) => void;
  /** A refusal for this control: the server's 400 under `diseases`, or the dialog's own. */
  error?: string;
  disabled?: boolean;
}

/**
 * Whose record a field is part of (#2332): the base record every patient
 * has, or the extension of one or more Disease Categories. A membership edit
 * and nothing else: it changes no stored value and no mapping.
 */
export function FieldScopeControl({ scope, diseases, options, onChange, error, disabled }: Props) {
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
      className="mb-4 rounded-md border border-slate-200 p-4"
      aria-describedby={error ? "field-scope-error" : undefined}
    >
      <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
        Scope — whose record this field is part of
      </legend>
      <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm text-slate-700">
        <label className="inline-flex items-center gap-2">
          <input
            type="radio"
            name="field-scope"
            checked={scope === "base"}
            disabled={disabled}
            onChange={() => onChange("base", diseases)}
          />
          Base record (every patient)
        </label>
        <label className="inline-flex items-center gap-2">
          <input
            type="radio"
            name="field-scope"
            checked={scope === "disease"}
            disabled={disabled}
            onChange={() => onChange("disease", diseases)}
          />
          Disease-specific
        </label>
      </div>
      {scope === "disease" && (
        <div role="group" aria-label="Disease Categories" className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-sm">
          {all.length === 0 && (
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
      {error && (
        <p id="field-scope-error" role="alert" className="mt-2 text-sm text-red-600">
          {error}
        </p>
      )}
    </fieldset>
  );
}
