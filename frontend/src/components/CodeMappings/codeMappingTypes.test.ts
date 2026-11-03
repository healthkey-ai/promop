import { describe, expect, it } from "vitest";
import { buildEditForm, emptyReference, type CodeMappingRow } from "./codeMappingTypes";

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
