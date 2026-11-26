import { describe, expect, it } from "vitest";
import { BASE_FILTER, canHaveScope, inDiseaseView, recordDiseaseCategories, resolveDiseaseFilter } from "./fieldScope";

const BASE = { scope: "base" as const, diseases: [] };
const MM = { scope: "disease" as const, diseases: ["MM"] };
const SHARED = { scope: "disease" as const, diseases: ["FL", "MM"] };
const BC = { scope: "disease" as const, diseases: ["BC"] };
// A descriptor from an instance older than #2331 carries neither key.
const UNSCOPED = {};

describe("inDiseaseView", () => {
  // The rule of disease_field_sets.disease_set on the server.
  it.each([
    ["every field with no filter", "", "extension", [true, true, true, true, true]],
    ["the base record", BASE_FILTER, "extension", [true, false, false, false, true]],
    ["the base record whatever the set", BASE_FILTER, "profile", [true, false, false, false, true]],
    ["a disease's extension", "MM", "extension", [false, true, true, false, false]],
    ["a disease's profile", "MM", "profile", [true, true, true, false, true]],
    ["nothing for a code no field names", "CLL", "extension", [false, false, false, false, false]],
    ["the base record alone as the profile of such a code", "CLL", "profile", [true, false, false, false, true]],
  ] as const)("lists %s", (_name, filter, set, expected) => {
    expect([BASE, MM, SHARED, BC, UNSCOPED].map((f) => inDiseaseView(f, filter, set))).toEqual(expected);
  });
});

describe("resolveDiseaseFilter", () => {
  const options = [
    { value: "MM", label: "Multiple Myeloma" },
    { value: "DLBCL", label: "Diffuse Large B-Cell Lymphoma" },
  ];

  it.each([
    ["", ""],
    ["  ", ""],
    [BASE_FILTER, BASE_FILTER],
    ["MM", "MM"],
    ["mm", "MM"],
    [" dlbcl ", "DLBCL"],
    ["multiple myeloma", "MM"],
    ["Other", BASE_FILTER],
    ["other", BASE_FILTER],
  ])("reads %j as %j", (value, expected) => {
    expect(resolveDiseaseFilter(value, options)).toBe(expected);
  });

  it("knows no category for anything else", () => {
    expect(resolveDiseaseFilter("XYZ", options)).toBeNull();
    expect(resolveDiseaseFilter("MM", [])).toBeNull();
  });
});

describe("canHaveScope", () => {
  it("refuses only the fields that decide a record's profile", () => {
    expect(["disease", "disease_slug", "disease_slugs"].map(canHaveScope)).toEqual([false, false, false]);
    expect(["stage", "disease_status", "genetic_mutations.origin"].map(canHaveScope)).toEqual([true, true, true]);
  });
});

describe('recordDiseaseCategories', () => {
  it('lists the primary first, then the others, without repeats, Other or blanks', () => {
    expect(recordDiseaseCategories('MM', ['PC', 'MM', 'Other', ''])).toEqual(['MM', 'PC']);
    expect(recordDiseaseCategories('', ['PC'])).toEqual(['PC']);
    expect(recordDiseaseCategories('MM')).toEqual(['MM']);
    expect(recordDiseaseCategories('', null)).toEqual([]);
  });
});
