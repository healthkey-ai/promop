import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { CustomPatientFields } from "./CustomPatientFields";
import { FieldMappingLinksProvider } from "./fieldMappingLinks";

const mockedApi = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@/api/axios", () => ({ default: mockedApi }));

const field = (id: number, field_name: string, tab: string, diseases: string[] = [], extra = {}) => ({
  id, field_name, display_name: field_name.replace(/_/g, " "), tab,
  field_type: "text", mode: "editable", unit: "",
  scope: diseases.length ? "disease" : "base", diseases, ...extra,
});

const FIELDS = [
  field(1, "tumor_note", "disease", ["MM"]),
  field(2, "her2_note", "disease", ["BC"]),
  field(3, "frailty_index", "labs", [], { field_type: "number", unit: "points" }),
  field(4, "transfusions", "blood"),
  field(5, "consented", "general", [], { field_type: "boolean" }),
];

describe("CustomPatientFields", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedApi.get.mockResolvedValue({ data: FIELDS });
  });

  it("shows the added fields of the tab that are part of this record, with their values", async () => {
    render(<CustomPatientFields tabs={["disease"]} diseases={["MM"]} formData={{ custom_fields: { tumor_note: "Stable" } }} />);
    expect(await screen.findByText("tumor note")).toBeInTheDocument();
    expect(screen.getByText("Stable")).toBeInTheDocument();
    // Another disease's field, and another tab's, are not this tab's.
    expect(screen.queryByText("her2 note")).not.toBeInTheDocument();
    expect(screen.queryByText("frailty index")).not.toBeInTheDocument();
    expect(mockedApi.get).toHaveBeenCalledWith("/v1/custom-patient-fields/");
  });

  it("shows a base field to every record and a blood field on the labs tab", async () => {
    render(<CustomPatientFields tabs={["labs", "blood"]} diseases={[]} formData={{ custom_fields: { frailty_index: 4 } }} />);
    expect(await screen.findByText("frailty index")).toBeInTheDocument();
    expect(screen.getByText("4 points")).toBeInTheDocument();
    expect(screen.getByText("transfusions")).toBeInTheDocument();
    expect(screen.getByText("Not recorded")).toBeInTheDocument();
  });

  it("reads a boolean as Yes or No", async () => {
    render(<CustomPatientFields tabs={["general"]} diseases={[]} formData={{ custom_fields: { consented: false } }} />);
    expect(await screen.findByText("consented")).toBeInTheDocument();
    expect(screen.getByText("No")).toBeInTheDocument();
  });

  it("renders nothing for a tab with no added field, or a record of another disease", async () => {
    const { container, rerender } = render(<CustomPatientFields tabs={["treatment"]} diseases={["MM"]} formData={{}} />);
    await waitFor(() => expect(mockedApi.get).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
    rerender(<CustomPatientFields tabs={["disease"]} diseases={[]} formData={{}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when the definitions cannot be read", async () => {
    mockedApi.get.mockRejectedValue(new Error("offline"));
    const { container } = render(<CustomPatientFields tabs={["disease"]} diseases={["MM"]} formData={{}} />);
    await waitFor(() => expect(mockedApi.get).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("gives each field its globe only where field mapping links are on", async () => {
    render(
      <MemoryRouter>
        <FieldMappingLinksProvider value>
          <CustomPatientFields tabs={["disease"]} diseases={["MM"]} formData={{}} />
        </FieldMappingLinksProvider>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("link", { name: "Field concept mapping for tumor note" }))
      .toHaveAttribute("href", "/field-mappings?field=tumor_note");
  });
});
