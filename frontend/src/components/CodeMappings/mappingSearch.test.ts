import { describe, expect, it } from "vitest";
import type { CodeMappingRow } from "./codeMappingTypes";
import { parseSearch, rowMatchesSearch } from "./mappingSearch";

const row = {
  source_code: "6784",
  source_vocabulary_id: "urn:oid:1.2.840.114350.1.13.300.2.7.5.737384.162",
  source_code_description: "Monoclonal protein",
  destination_concept_name: "Protein.monoclonal [Mass/volume] in Serum",
  destination_concept_code: "33358-3",
  destination_concept_id: 3004061,
  organization_name: "Hospital A",
  organization_slug: "hospital-a",
} as unknown as CodeMappingRow;

describe("mapping search", () => {
  it("a number matches only the source code, never the OID code system", () => {
    expect(rowMatchesSearch(row, "6784")).toBe(true);
    expect(rowMatchesSearch(row, "114350")).toBe(false);
    expect(rowMatchesSearch(row, "3004061")).toBe(false);
  });

  it("text still searches every field", () => {
    expect(rowMatchesSearch(row, "monoclonal")).toBe(true);
    expect(rowMatchesSearch(row, "urn:oid:1.2.840")).toBe(true);
    expect(rowMatchesSearch(row, "hospital-a")).toBe(true);
    expect(rowMatchesSearch(row, "nothing here")).toBe(false);
  });

  it("field:value searches one field, by any alias", () => {
    expect(parseSearch(" Code : 6784 ")).toEqual({ field: "source_code", value: "6784" });
    expect(rowMatchesSearch(row, "source_code_value:6784")).toBe(true);
    expect(rowMatchesSearch(row, "code:monoclonal")).toBe(false);
    expect(rowMatchesSearch(row, "description:monoclonal")).toBe(true);
    expect(rowMatchesSearch(row, "system:114350")).toBe(true);
    expect(rowMatchesSearch(row, "concept:serum")).toBe(true);
    expect(rowMatchesSearch(row, "concept_code:33358")).toBe(true);
    expect(rowMatchesSearch(row, "org:hospital")).toBe(true);
  });

  it("concept_id is exact", () => {
    expect(rowMatchesSearch(row, "concept_id:3004061")).toBe(true);
    expect(rowMatchesSearch(row, "concept_id:300406")).toBe(false);
    expect(rowMatchesSearch(row, "concept_id:abc")).toBe(false);
    expect(rowMatchesSearch(row, "concept_id:00000003004061")).toBe(false); // over ten digits, as on the server
  });

  it("an unknown prefix or an empty value is plain text", () => {
    expect(parseSearch("urn:oid:1")).toEqual({ field: null, value: "urn:oid:1" });
    expect(parseSearch("code:")).toEqual({ field: null, value: "code:" });
    expect(rowMatchesSearch(row, "")).toBe(true);
  });
});
