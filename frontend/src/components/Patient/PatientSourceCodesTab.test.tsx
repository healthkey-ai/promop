import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import api from "../../api/axios";
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
    source_code_id: 44,
    source_unit: "mg/dL",
    example_quantity: "12",
    extracted_source_unit: "bad unit",
    extracted_example_quantity: "12",
    source_unit_override: "mg/dL",
    example_quantity_override: null,
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
    get: vi.fn((url: string) => Promise.resolve({
      data: url.includes("source-codes") ? sourceCodes : {},
    })),
    post: vi.fn(() => Promise.resolve({ data: {} })),
    patch: vi.fn(() => Promise.resolve({ data: {} })), delete: vi.fn(() => Promise.resolve({})),
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

it("links an Epic patient source system to its exact hospital-management group", async () => {
  const oid = "urn:oid:1.2.840.114350.1.13.90.2.7.2.707679";
  const getSpy = vi.spyOn(api, "get").mockImplementation((url: string) => Promise.resolve({
    data: url.includes("source-codes") ? {
      ...sourceCodes, source_codes: [{ ...sourceCodes.source_codes[0], source_vocabulary_id: oid }],
    } : {},
  }) as never);
  try {
    render(<PatientSourceCodesTab personId="48095" />);
    fireEvent.click(await screen.findByText("Blood Pressure"));
    const systemLink = screen.getByRole("link", { name: oid });
    expect(systemLink).toHaveAttribute("href", `/code-mappings/source-systems?system=${encodeURIComponent(oid)}`);
    expect(systemLink).toHaveAttribute("title", expect.stringContaining("review and manage facilities"));
    expect(screen.queryByRole("link", { name: "Manage hospitals for this source code system" })).not.toBeInTheDocument();
  } finally {
    getSpy.mockRestore();
  }
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

it("sends the current patient when saving a mapping", async () => {
  vi.mocked(api.patch).mockResolvedValueOnce({ data: { mapping_id: 78 } } as never);
  render(<PatientSourceCodesTab personId="48095" />);
  fireEvent.click(await screen.findByText("Pulse"));
  fireEvent.change(screen.getByLabelText("Status"), { target: { value: "approved" } });
  fireEvent.submit(screen.getByRole("button", { name: "Update Mapping" }).closest("form")!);
  await waitFor(() => expect(api.patch).toHaveBeenCalledWith(
    "/v1/code-mappings/78/",
    expect.objectContaining({ patient_id: 48095, status: "approved" }),
  ));
});

describe("PatientSourceCodesTab source context correction", () => {
  it("lets a curator compare FHIR metadata and save a source-unit correction separately", async () => {
    render(<PatientSourceCodesTab personId="48095" />);
    fireEvent.click(await screen.findByText("Blood Pressure"));
    fireEvent.click(screen.getByRole("button", { name: /Source Metadata/ }));
    expect(screen.getByText(/Vital signs/)).toBeInTheDocument();
    expect(screen.getByText(/Extracted: bad unit/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Search UCUM units" })).toHaveValue("mg/dL");
    fireEvent.change(screen.getByLabelText(/^Source Unit/), { target: { value: "cells/uL" } });
    expect(screen.getByRole("textbox", { name: "Search UCUM units" })).toHaveValue("cells/uL");
    fireEvent.click(screen.getByRole("button", { name: "Save source context" }));
    await waitFor(() => expect(api.patch).toHaveBeenCalledWith(
      "/v1/patient-records/48095/source-codes/",
      { source_code_id: 44, source_unit_override: "cells/uL", example_quantity_override: null },
    ));
    expect(await screen.findByText(/Source context saved/)).toBeInTheDocument();
  });

  it("drops an auto-selected destination unit when the source unit changes", async () => {
    const getSpy = vi.spyOn(api, "get").mockImplementation((url: string) => Promise.resolve({
      data: url.includes("unit-search")
        ? { results: [{ concept_id: 8840, concept_code: "mg/dL", concept_name: "milligrams per deciliter", match: "exact" }] }
        : url.includes("source-codes") ? sourceCodes : {},
    }) as never);
    try {
      render(<PatientSourceCodesTab personId="48095" />);
      fireEvent.click(await screen.findByText("Blood Pressure"));
      await waitFor(() => expect(screen.queryByRole("textbox", { name: "Search UCUM units" })).not.toBeInTheDocument());
      fireEvent.change(screen.getByLabelText(/^Source Unit/), { target: { value: "cells/uL" } });
      expect(screen.getByRole("textbox", { name: "Search UCUM units" })).toHaveValue("cells/uL");
    } finally {
      getSpy.mockRestore();
    }
  });

  it("keeps a manually chosen destination unit but asks for review after a source correction", async () => {
    const getSpy = vi.spyOn(api, "get").mockImplementation((url: string) => Promise.resolve({
      data: url.includes("unit-search")
        ? { results: [{ concept_id: 8840, concept_code: "mg/dL", concept_name: "milligrams per deciliter", match: "close" }] }
        : url.includes("source-codes") ? sourceCodes : {},
    }) as never);
    try {
      render(<PatientSourceCodesTab personId="48095" />);
      fireEvent.click(await screen.findByText("Blood Pressure"));
      fireEvent.click(await screen.findByText("milligrams per deciliter"));
      fireEvent.change(screen.getByLabelText(/^Source Unit/), { target: { value: "cells/uL" } });
      expect(screen.getByText("milligrams per deciliter")).toBeInTheDocument();
      expect(screen.getByText(/Source unit changed. Review this destination unit/)).toBeInTheDocument();
    } finally {
      getSpy.mockRestore();
    }
  });
});
