import { describe, expect, it, vi } from "vitest";
import {
  buildEditForm, confidencePercent, emptyReference, pollSuggestRun, rankedConfidence,
  type CodeMappingRow, type SuggestRunProgress,
} from "./codeMappingTypes";

const row = (destination: number | null) => ({
  mapping_id: 1, domain_id: "Measurement", source_vocabulary_id: "LOINC", source_code: "9279-1",
  source_code_description: "", destination_concept_id: destination, destination_concept_name: "",
  destination_concept_code: "", destination_vocabulary_id: "", destination_concept_class_id: "",
  destination_omop_table: "measurement", destination_domain_id: "Measurement", status: "proposed",
  notes: "", origin: "", origin_system: "", suggest_strategy: "", umls_cui: "", created_by: "",
  occurrence_count: 1, destination_count: 0, has_mapping: true,
}) as CodeMappingRow;

describe("buildEditForm destination", () => {
  it.each([null, 0])("treats %s as no destination", (destination) => {
    expect(buildEditForm(row(destination), emptyReference).destination_concept_id).toBe("");
  });

  it("keeps a real destination", () => {
    expect(buildEditForm(row(3024171), emptyReference).destination_concept_id).toBe("3024171");
  });
});

describe("the form's confidence (#2668)", () => {
  it("carries the stored one for a destination, and none for a row with no destination", () => {
    expect(buildEditForm({ ...row(3024171), suggestion_confidence: 0.82 }, emptyReference).suggestion_confidence).toBe(0.82);
    expect(buildEditForm({ ...row(null), suggestion_confidence: 0.82 }, emptyReference).suggestion_confidence).toBeNull();
  });

  it("is left undefined, not cleared, when the row did not say", () => {
    expect(buildEditForm(row(3024171), emptyReference).suggestion_confidence).toBeUndefined();
  });

  it("is shown as a whole percent, or nothing", () => {
    expect(confidencePercent(0.824)).toBe("82");
    expect(confidencePercent(1)).toBe("100");
    expect(confidencePercent(0)).toBe("0");
    expect(confidencePercent(null)).toBe("");
    expect(confidencePercent(undefined)).toBe("");
  });
});

describe("pollSuggestRun", () => {
  const run = (state: "queued" | "running" | "success" | "failure") => ({ run_id: "r", state }) as SuggestRunProgress;

  it("returns a run that is already finished without polling", async () => {
    const poll = vi.fn();
    expect((await pollSuggestRun(run("success"), poll, () => true))?.state).toBe("success");
    expect(poll).not.toHaveBeenCalled();
  });

  it("returns null once the caller has moved on", async () => {
    expect(await pollSuggestRun(run("running"), vi.fn(), () => false)).toBeNull();
  });

  it("gives up after repeated poll failures", async () => {
    vi.useFakeTimers();
    const poll = vi.fn().mockRejectedValue(new Error("down"));
    const settled = pollSuggestRun(run("running"), poll, () => true).catch((e) => e);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(await settled).toBeInstanceOf(Error);
    vi.useRealTimers();
  });
});

describe("rankedConfidence (#2382)", () => {
  const activity = [
    { stage: "candidates", alternatives: [{ concept_id: 1, confidence: 0.2 }] },
    { stage: "ranked", alternatives: [{ concept_id: 1, confidence: 0.9, ranker: "anthropic" }, { concept_id: 2, confidence: 0.3, ranker: "anthropic" },
      { concept_id: 2, confidence: 0.8, ranker: "jev" }, { concept_id: 3, confidence: 1.5 }] },
    { stage: "result", suggested: { concept_id: 1 } },
  ];
  it("reads the last event that carries scores", () => {
    expect(rankedConfidence(activity, "1")).toBe(0.9);
  });
  it("takes the higher ranker's score for a concept both scored, as the table shows", () => {
    expect(rankedConfidence(activity, "2")).toBe(0.8);
  });
  it("treats a score the server would refuse, an unscored concept or no concept as unscored", () => {
    expect(rankedConfidence(activity, "3")).toBeNull();
    expect(rankedConfidence(activity, "4")).toBeNull();
    expect(rankedConfidence(activity, "")).toBeNull();
    expect(rankedConfidence([], "1")).toBeNull();
  });
});
