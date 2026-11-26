import { describe, expect, it } from "vitest";
import { mergeAddedFields, pickAddedFields } from "./addedFieldEdits";

describe("added field edits", () => {
  it("picks the named keys, a missing one as a cleared value", () => {
    expect(pickAddedFields({ a: 1, b: "x", c: false }, ["a", "c", "d"])).toEqual({ a: 1, c: false, d: null });
    expect(pickAddedFields(undefined, ["a"])).toEqual({ a: null });
    expect(pickAddedFields({ a: 1 }, [])).toEqual({});
  });

  it("replaces keys without touching the others", () => {
    const base = { a: 1, b: 2 };
    expect(mergeAddedFields(base, { b: null, c: 3 })).toEqual({ a: 1, b: null, c: 3 });
    expect(base).toEqual({ a: 1, b: 2 });
    expect(mergeAddedFields(null, { a: 1 })).toEqual({ a: 1 });
    expect(mergeAddedFields(["not", "an", "object"], undefined)).toEqual({});
  });
});
