import { describe, expect, it } from "vitest";
import { hospitalContextDetails, hospitalContextText } from "./sourceSystemContext";

describe("hospital source-system context", () => {
  it("distinguishes a verified organization from an observed facility", () => {
    expect(hospitalContextText({ kind: "verified", label: "Hospital A", names: [], name_count: 0 }))
      .toBe("Verified: Hospital A");
    expect(hospitalContextText({ kind: "observed", label: "UPMC", names: ["UPMC"], name_count: 1 }))
      .toBe("Observed at: UPMC");
  });

  it("does not collapse several facilities into one hospital", () => {
    const context = { kind: "multiple" as const, label: "Multiple observed facilities",
      names: ["UPMC", "Hillman Cancer Center"], name_count: 3 };
    expect(hospitalContextText(context)).toBe("Multiple observed facilities (3)");
    expect(hospitalContextDetails(context)).toContain("UPMC, Hillman Cancer Center, and others");
  });

  it("does not assert an unknown hospital", () => {
    expect(hospitalContextText()).toBe("Global / hospital unverified");
  });
});
