import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import PatientSourceCodesTab from "./PatientSourceCodesTab";

// A doctor without the analyst grant reads the patient's codes and maps none (#2416).
vi.mock("../../hooks/useAuth", () => ({
  useAuth: () => ({
    currentUser: {
      id: 9,
      email: "doctor@example.com",
      is_staff: false,
      org_accesses: [{ org_slug: "acme", org_name: "Acme", role: "doctor", expires_at: null }],
    },
  }),
}));

vi.mock("../../api/axios", () => ({
  default: {
    get: vi.fn((url: string) => Promise.resolve({
      data: url.includes("source-codes")
        ? {
            person_id: 48095,
            summary: { total: 1, unmapped: 1, proposed: 0, approved: 0 },
            source_codes: [{
              omop_table: "measurement", source_value: "8716-3", source_vocabulary_id: "LOINC",
              source_code_description: "Blood Pressure", row_count: 3, mapping_status: "unmapped",
              mapping_id: null, destination_concept_id: null, destination_concept_name: null,
            }],
          }
        : {},
    })),
    post: vi.fn(() => Promise.resolve({ data: {} })),
    patch: vi.fn(() => Promise.resolve({ data: {} })),
    delete: vi.fn(() => Promise.resolve({})),
  },
}));

describe("PatientSourceCodesTab for a reader (#2416)", () => {
  it("lists the codes, says it is read-only, and opens no dialog on a row click", async () => {
    render(<PatientSourceCodesTab personId="48095" />);
    const code = await screen.findByText("8716-3");
    expect(screen.getByRole("status")).toHaveTextContent("Read-only: mapping codes needs the analyst role.");
    const row = code.closest("tr");
    expect(row).not.toHaveClass("cursor-pointer");
    fireEvent.click(code);
    fireEvent.click(screen.getByText("Blood Pressure"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Source Description/)).not.toBeInTheDocument();
  });
});
