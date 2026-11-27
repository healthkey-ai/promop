import { useEffect, useId, useRef, useState } from "react";
import { Plus, Trash2, X } from "lucide-react";
import api from "@/api/axios";
import { HelpTip, INPUT_CLASS } from "@/components/UI/MappingFormPrimitives";

import {
  blankAnswer, isEntered, type AnswerConcept, type AnswerDraft, type AnswerErrors,
} from "./answerDrafts";

const TIP =
  "The answers a person chooses from, in this order. The label is what they read. The stored value is "
  + "what is saved with the record (the label when left blank) and does not change if the label is "
  + "renamed later. An answer's concept, when it has one, is written beside the value as the answer "
  + "concept of the field's OMOP fact. More can be added, renamed and mapped later from Field Mappings.";

/** A search for the concept an answer means; one answer's own, so each keeps its text. */
function ConceptPicker({ id, concept, disabled, onChange }: {
  id: string;
  concept: AnswerConcept | null;
  disabled?: boolean;
  onChange: (concept: AnswerConcept | null) => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<AnswerConcept[]>([]);
  const [searching, setSearching] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    if (query.trim().length < 3) {
      timer.current = null;
      return undefined;
    }
    let current = true;
    timer.current = setTimeout(() => {
      (async () => {
        setSearching(true);
        try {
          const response = await api.get("/v1/concepts/search/", { params: { q: query.trim(), limit: "8" } });
          const body = response.data;
          if (current) setResults(Array.isArray(body) ? body : body?.results ?? []);
        } catch {
          if (current) setResults([]);
        } finally {
          if (current) setSearching(false);
        }
      })();
    }, 300);
    return () => {
      current = false;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [query]);

  if (concept) {
    return (
      <div className="flex h-10 items-center justify-between gap-2 rounded-md bg-slate-50 px-3 text-xs text-slate-700">
        <span className="truncate" title={`${concept.vocabulary_id}:${concept.concept_code} ${concept.concept_name}`}>
          <span className="font-mono">{concept.vocabulary_id}:{concept.concept_code}</span> {concept.concept_name}
        </span>
        <button
          type="button"
          disabled={disabled}
          onClick={() => onChange(null)}
          aria-label={`Remove concept ${concept.concept_name}`}
          className="shrink-0 rounded p-0.5 text-slate-400 hover:text-slate-600"
        >
          <X size={13} />
        </button>
      </div>
    );
  }
  const shown = query.trim().length >= 3 ? results : [];
  return (
    <div className="relative">
      <input
        id={id}
        type="text"
        value={query}
        disabled={disabled}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search a concept (optional)"
        className={INPUT_CLASS}
      />
      {query.trim().length >= 3 && (
        <div className="absolute z-10 mt-1 max-h-44 w-full overflow-y-auto rounded-md border border-slate-200 bg-white shadow-md">
          {searching && <div className="px-3 py-2 text-xs text-slate-500">Searching...</div>}
          {!searching && shown.length === 0 && <div className="px-3 py-2 text-xs text-slate-500">No results found.</div>}
          {shown.map((result) => (
            <button
              key={result.concept_id}
              type="button"
              onClick={() => {
                onChange(result);
                setQuery("");
                setResults([]);
              }}
              className="grid w-full grid-cols-[7rem_1fr_5rem] gap-2 border-b border-slate-100 px-3 py-1.5 text-left text-xs last:border-0 hover:bg-slate-50"
            >
              <span className="font-mono text-slate-700">{result.concept_code}</span>
              <span className="text-slate-900">{result.concept_name}</span>
              <span className="font-mono text-slate-500">{result.vocabulary_id}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * The answers of a dropdown field being created: a label, the value
 * stored for it (the label when blank) and, optionally, the standard concept
 * it means. They become the field's `FieldChoice` rows, the ones the Field
 * Mappings page edits afterwards, so this is where a field's list starts and
 * not a second place it lives.
 */
export function NewFieldAnswers({ answers, errors, disabled, onChange }: {
  answers: AnswerDraft[];
  /** The server's refusal of each row, by position among the rows sent. */
  errors?: Record<number, AnswerErrors>;
  disabled?: boolean;
  onChange: (answers: AnswerDraft[]) => void;
}) {
  const heading = useId();
  const update = (key: number, change: Partial<AnswerDraft>) => (
    onChange(answers.map((answer) => (answer.key === key ? { ...answer, ...change } : answer)))
  );
  // The server counts the rows it was sent, which leave out the rows with nothing entered.
  const sent = answers.filter(isEntered);
  return (
    <fieldset className="mb-4 rounded-md border border-slate-200 p-4" aria-labelledby={heading}>
      <legend id={heading} className="px-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
        <span className="inline-flex items-center gap-1">Answers <HelpTip tip={TIP} /></span>
      </legend>
      <div className="grid gap-3">
        {answers.map((answer, index) => {
          const sentIndex = sent.findIndex((candidate) => candidate.key === answer.key);
          const refused = sentIndex >= 0 ? errors?.[sentIndex] : undefined;
          return (
            <div key={answer.key} data-testid="new-answer" className="grid gap-2 md:grid-cols-[1fr_9rem_1fr_auto] md:items-start">
              <div>
                <input
                  type="text"
                  aria-label={`Answer ${index + 1} label`}
                  placeholder="Label, e.g. Grade group 2"
                  value={answer.label}
                  disabled={disabled}
                  onChange={(e) => update(answer.key, { label: e.target.value })}
                  className={INPUT_CLASS}
                />
                {refused?.label && <p role="alert" className="mt-1 text-xs text-red-600">{refused.label}</p>}
              </div>
              <div>
                <input
                  type="text"
                  aria-label={`Answer ${index + 1} stored value`}
                  placeholder="Stored value"
                  value={answer.value}
                  disabled={disabled}
                  onChange={(e) => update(answer.key, { value: e.target.value })}
                  className={`${INPUT_CLASS} font-mono`}
                />
                {refused?.value && <p role="alert" className="mt-1 text-xs text-red-600">{refused.value}</p>}
              </div>
              <div>
                <ConceptPicker
                  id={`new-answer-concept-${answer.key}`}
                  concept={answer.concept}
                  disabled={disabled}
                  onChange={(concept) => update(answer.key, { concept })}
                />
                {refused?.concept && <p role="alert" className="mt-1 text-xs text-red-600">{refused.concept}</p>}
              </div>
              <button
                type="button"
                disabled={disabled || answers.length === 1}
                onClick={() => onChange(answers.filter((candidate) => candidate.key !== answer.key))}
                aria-label={`Remove answer ${index + 1}`}
                className="mt-2 rounded p-1 text-slate-400 hover:text-slate-600 disabled:opacity-40"
              >
                <Trash2 size={15} />
              </button>
            </div>
          );
        })}
      </div>
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange([...answers, blankAnswer()])}
        className="mt-3 inline-flex items-center gap-1 rounded-md border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100"
      >
        <Plus size={13} />
        Add answer
      </button>
    </fieldset>
  );
}
