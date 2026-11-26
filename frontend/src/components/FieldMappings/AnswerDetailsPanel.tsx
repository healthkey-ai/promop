import { useState } from "react";
import api from "@/api/axios";
import type { DiseaseCategoryOption } from "@/types/fieldScope";
import { refusalText } from "./answerConcept";
import { FieldScopeControl } from "./FieldScopeControl";

const scopeWords = (label: string) => ({
  legend: `Scope — which patients are offered "${label}"`,
  base: "Every patient",
  disease: "Only some Disease Categories",
  tip:
    "Every patient: the answer is offered wherever the field is. Only some Disease Categories: it is offered "
    + "only to records of the ticked categories. A value already saved is never changed or hidden by this.",
});

interface Props {
  choiceId: number;
  label: string;
  /** The stable identity; it never changes when the label does. */
  code?: string;
  /** What a save writes for the answer; it stays as it is when the label is renamed. */
  storedValue?: string;
  aliases?: string[];
  retired?: boolean;
  /** Disease Category codes the answer is offered to; none means every patient. */
  diseases?: string[];
  /** Separates answers that share a label, such as one staging system from another. */
  contextKey?: string;
  /** The categories a scope can name; without them the scope is shown but not edited. */
  diseaseOptions?: DiseaseCategoryOption[];
  optionsIncomplete?: boolean;
  /** Org admins and staff change what is offered at once; a curator sees it. */
  canEdit?: boolean;
  /** Called after any change, so the list is read again. */
  onChanged: () => Promise<void> | void;
}

const clean = (text: string): string[] => {
  const seen = new Set<string>();
  for (const line of text.split("\n")) {
    const alias = line.split(/\s+/).filter(Boolean).join(" ");
    if (alias) seen.add(alias);
  }
  return [...seen];
};

/** The label, other spellings and standing of one answer (#1225).
 *
 *  The label is what patients see; what a save stores is the answer's value,
 *  frozen at the first rename so saves before and after it agree, and shown
 *  here read only. A rename keeps the old label as another spelling (the
 *  server does that) and nothing already saved stops matching. Retiring stops offering the answer without deleting it or its
 *  concept decision. */
const sameSet = (a: string[], b: string[]) => [...a].sort().join("\n") === [...b].sort().join("\n");

export function AnswerDetailsPanel({
  choiceId, label, code, storedValue, aliases = [], retired = false, diseases = [], contextKey = "",
  diseaseOptions, optionsIncomplete = false, canEdit = true, onChanged,
}: Props) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(label);
  const [spellings, setSpellings] = useState(aliases.join("\n"));
  // What the form was opened on: only what the curator changed from it is
  // sent, so an unedited box never writes back a stale copy of the other one.
  const [baseline, setBaseline] = useState({ label, aliases, diseases });
  const [scope, setScope] = useState<"base" | "disease">(diseases.length ? "disease" : "base");
  const [ticked, setTicked] = useState<string[]>(diseases);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const patch = async (body: Record<string, unknown>, failure: string): Promise<boolean> => {
    if (busy) return false;
    setBusy(true);
    setError("");
    try {
      await api.patch(`/v1/field-choices/${choiceId}/`, body);
      await onChanged();
      return true;
    } catch (e) {
      setError(refusalText(e, failure));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const open = () => {
    setName(label);
    setSpellings(aliases.join("\n"));
    setBaseline({ label, aliases, diseases });
    setScope(diseases.length ? "disease" : "base");
    setTicked(diseases);
    setError("");
    setEditing(true);
  };

  const save = async () => {
    const body: Record<string, unknown> = {};
    const trimmed = name.split(/\s+/).filter(Boolean).join(" ");
    if (!trimmed) {
      setError("The label cannot be empty.");
      return;
    }
    if (trimmed !== baseline.label) body.display = trimmed;
    const next = clean(spellings);
    if (next.join("\n") !== baseline.aliases.join("\n")) body.aliases = next;
    const wanted = scope === "base" ? [] : ticked;
    if (scope === "disease" && wanted.length === 0) {
      setError("Tick at least one Disease Category, or offer the answer to every patient.");
      return;
    }
    if (diseaseOptions && !sameSet(wanted, baseline.diseases)) body.diseases = wanted;
    if (Object.keys(body).length === 0) {
      setEditing(false);
      return;
    }
    if (await patch(body, "The change was not saved.")) setEditing(false);
  };

  const toggleRetired = () => {
    if (!retired && !window.confirm(
      `Retire "${label}"? It stops being offered for new answers. Values already saved, and its concept decision, are kept.`)) return;
    void patch({ retired: !retired }, retired ? "The answer was not restored." : "The answer was not retired.");
  };

  return (
    <div role="group" aria-label={`Details of ${label}`} className="mt-1 text-xs text-gray-600">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
        {retired && (
          <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[10px] font-medium text-gray-700">Retired</span>
        )}
        {code && <span>Code <span className="font-mono">{code}</span></span>}
        {storedValue && (
          <span title="What a save writes for this answer. Renaming the label never changes it.">
            Saved as <span className="font-mono">{storedValue}</span>
          </span>
        )}
        {aliases.length > 0 && <span>Also written: {aliases.join(", ")}</span>}
        {contextKey && <span>Context <span className="font-mono">{contextKey}</span></span>}
        <span>{diseases.length ? `Offered to: ${diseases.join(", ")}` : "Offered to every patient"}</span>
        {canEdit && (
          <>
        <button type="button" disabled={busy} onClick={editing ? () => setEditing(false) : open}
          aria-expanded={editing}
          aria-label={editing ? `Cancel editing ${label}` : `Edit details of ${label}`}
          className="rounded px-1 py-0.5 text-gray-500 underline hover:text-gray-700 disabled:opacity-50">
          {editing ? "Cancel" : "Edit details"}
        </button>
        <button type="button" disabled={busy || editing} onClick={toggleRetired}
          aria-label={`${retired ? "Restore" : "Retire"} ${label}`}
          className="rounded px-1 py-0.5 text-gray-500 underline hover:text-gray-700 disabled:opacity-50">
          {retired ? "Restore" : "Retire"}
        </button>
          </>
        )}
      </div>
      {error && <div role="alert" className="mt-1 text-red-600">{error}</div>}
      {canEdit && editing && (
        <div className="mt-1 space-y-1 rounded border border-gray-200 bg-white p-2">
          <label className="block">
            <span className="text-gray-500">Label</span>
            <input type="text" value={name} onChange={(e) => setName(e.target.value)}
              aria-label="Label"
              className="mt-0.5 h-7 w-full rounded border border-gray-300 px-2 text-sm" />
          </label>
          <label className="block">
            <span className="text-gray-500">Other spellings, one per line</span>
            <textarea value={spellings} onChange={(e) => setSpellings(e.target.value)}
              aria-label="Other spellings" rows={3}
              className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1 text-sm" />
          </label>
          {diseaseOptions && (
            <FieldScopeControl
              scope={scope}
              diseases={ticked}
              options={diseaseOptions}
              optionsIncomplete={optionsIncomplete}
              onChange={(next, codes) => { setScope(next); setTicked(codes); }}
              words={scopeWords(label)}
              disabled={busy}
            />
          )}
          <div className="text-[10px] text-gray-400">
            A new label keeps the old one as another spelling, so saved values and imports still match.
            The code stays the same.
          </div>
          <button type="button" disabled={busy || !name.trim()} onClick={save}
            className="rounded bg-primary px-2 py-0.5 text-primary-foreground disabled:opacity-50">
            {busy ? "Saving..." : "Save"}
          </button>
        </div>
      )}
    </div>
  );
}
