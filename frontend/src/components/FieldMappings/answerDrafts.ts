/** The concept an answer means, as the concept search returns it. */
export interface AnswerConcept {
  concept_id: number;
  concept_code: string;
  concept_name: string;
  vocabulary_id: string;
}

/** An answer being entered while a dropdown field is created. */
export interface AnswerDraft {
  /** Identity of the row in the editor; the server never sees it. */
  key: number;
  label: string;
  /** What is stored when the answer is chosen; blank stores the label. */
  value: string;
  concept: AnswerConcept | null;
}

/** What the server refused about one answer, by input. */
export interface AnswerErrors {
  label?: string;
  value?: string;
  concept?: string;
}

let nextKey = 1;

/** A new empty answer. */
export function blankAnswer(): AnswerDraft {
  return { key: nextKey++, label: "", value: "", concept: null };
}

/** A row with nothing entered is not an answer; one with a value or a concept but no label is, and
 *  the server says it needs a label. */
export const isEntered = (answer: AnswerDraft) => (
  answer.label.trim() !== "" || answer.value.trim() !== "" || answer.concept !== null
);

/** The answers as the create request sends them: the rows that have something entered. */
export function answersPayload(answers: AnswerDraft[]) {
  return answers
    .filter(isEntered)
    .map((answer) => ({
      label: answer.label.trim(),
      value: answer.value.trim(),
      concept: answer.concept?.concept_id ?? null,
    }));
}
