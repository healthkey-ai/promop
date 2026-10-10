import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import PatientSourceCodesTab from "./PatientSourceCodesTab";

vi.mock("../../hooks/useAuth", () => ({
  useAuth: () => ({ currentUser: { email: "curator@example.com", is_staff: true } }),
}));

const sourceCodes = {
  person_id: 48095,
  summary: { total: 3, unmapped: 1, proposed: 2, approved: 0 },
  source_codes: [{
    source_value: "8716-3",
    omop_table: "measurement",
    concept_id: 0,
    concept_name: null,
    row_count: 4,
    mapping_id: null,
    mapping_status: "unmapped",
    mapping_target_concept_id: null,
    mapping_target_concept_name: null,
    source_vocabulary_id: "LOINC",
    source_code: "8716-3",
    source_unit: "",
    example_quantity: "",
    source_metadata: { display: ["Vital signs"], text: ["Blood Pressure"] },
    source_code_description: "Blood Pressure",
  }, {
    // Proposed by ingest, no destination yet.
    source_value: "9279-1",
    omop_table: "measurement",
    concept_id: 0,
    concept_name: null,
    row_count: 2,
    mapping_id: 77,
    mapping_status: "proposed",
    mapping_target_concept_id: null,
    mapping_target_concept_name: null,
    source_vocabulary_id: "LOINC",
    source_code: "9279-1",
    source_unit: "",
    example_quantity: "",
    source_metadata: {},
    source_code_description: "Respirations",
  }, {
    // Proposed with a destination already.
    source_value: "8867-4",
    omop_table: "measurement",
    concept_id: 0,
    concept_name: null,
    row_count: 1,
    mapping_id: 78,
    mapping_status: "proposed",
    mapping_target_concept_id: 3027018,
    mapping_target_concept_name: "Heart rate",
    source_vocabulary_id: "LOINC",
    source_code: "8867-4",
    source_unit: "",
    example_quantity: "",
    source_metadata: {},
    source_code_description: "Pulse",
  }],
};

vi.mock("../../api/axios", () => ({
  default: {
    get: (url: string) => Promise.resolve({
      data: url.includes("source-codes") ? sourceCodes : {},
    }),
    post: vi.fn(() => Promise.resolve({ data: {} })),
    patch: vi.fn(), delete: vi.fn(() => Promise.resolve({})),
  },
}));

describe("PatientSourceCodesTab source description (#1778)", () => {
  it("shows the source's name beside the code", async () => {
    render(<PatientSourceCodesTab personId="48095" />);
    expect(await screen.findByText("8716-3")).toBeInTheDocument();
    expect(screen.getByText("Blood Pressure")).toBeInTheDocument();
  });

  it("starts the dialog's Source Description from it", async () => {
    render(<PatientSourceCodesTab personId="48095" />);
    fireEvent.click(await screen.findByText("Blood Pressure"));
    expect(await screen.findByLabelText(/Source Description/)).toHaveValue("Blood Pressure");
  });
});

describe("PatientSourceCodesTab missing destination", () => {
  it("opens a proposed row with no destination empty, never 0", async () => {
    render(<PatientSourceCodesTab personId="48095" />);
    fireEvent.click(await screen.findByText("Respirations"));
    const field = await screen.findByLabelText(/Destination Concept ID/);
    expect(field).toHaveValue(null);
    expect(field).not.toHaveValue(0);
    // Saving notes or a rejection must not demand a destination.
    expect(field).not.toBeRequired();
  });

  it("keeps an existing destination required, so clearing it cannot pass as a change", async () => {
    render(<PatientSourceCodesTab personId="48095" />);
    fireEvent.click(await screen.findByText("Pulse"));
    const field = await screen.findByLabelText(/Destination Concept ID/);
    expect(field).toHaveValue(3027018);
    expect(field).toBeRequired();
  });
});
