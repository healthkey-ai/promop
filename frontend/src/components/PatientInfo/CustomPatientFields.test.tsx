import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { CustomPatientFields } from "./CustomPatientFields";
import { FieldMappingLinksProvider } from "./fieldMappingLinks";
import type { CustomPatientField } from "@/hooks/useCustomPatientFields";

const field = (
  id: number, field_name: string, tab: string, diseases: string[] = [], extra: Partial<CustomPatientField> = {},
): CustomPatientField => ({
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
  it("shows the added fields of the tab that are part of this record, with their values", () => {
    render(<CustomPatientFields fields={FIELDS} tabs={["disease"]} diseases={["MM"]} formData={{ custom_fields: { tumor_note: "Stable" } }} />);
    expect(screen.getByRole("region", { name: "Additional fields" })).toBeInTheDocument();
    expect(screen.getByText("tumor note")).toBeInTheDocument();
    expect(screen.getByText("Stable")).toBeInTheDocument();
    // Another disease's field, and another tab's, are not this tab's.
    expect(screen.queryByText("her2 note")).not.toBeInTheDocument();
    expect(screen.queryByText("frailty index")).not.toBeInTheDocument();
  });

  it("shows a base field to every record and a blood field on the labs tab", () => {
    render(<CustomPatientFields fields={FIELDS} tabs={["labs", "blood"]} diseases={[]} formData={{ custom_fields: { frailty_index: 4 } }} />);
    expect(screen.getByText("frailty index")).toBeInTheDocument();
    expect(screen.getByText("4 points")).toBeInTheDocument();
    expect(screen.getByText("transfusions")).toBeInTheDocument();
    expect(screen.getByText("Not recorded")).toBeInTheDocument();
  });

  it("reads a boolean as Yes or No", () => {
    render(<CustomPatientFields fields={FIELDS} tabs={["general"]} diseases={[]} formData={{ custom_fields: { consented: false } }} />);
    expect(screen.getByText("consented")).toBeInTheDocument();
    expect(screen.getByText("No")).toBeInTheDocument();
  });

  it("renders nothing for a tab with no added field, a record of another disease, or no fields at all", () => {
    const { container, rerender } = render(<CustomPatientFields fields={FIELDS} tabs={["treatment"]} diseases={["MM"]} formData={{}} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<CustomPatientFields fields={FIELDS} tabs={["disease"]} diseases={[]} formData={{}} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<CustomPatientFields fields={[]} tabs={["disease"]} diseases={["MM"]} formData={{}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("gives each field its globe only where field mapping links are on", () => {
    const { unmount } = render(<CustomPatientFields fields={FIELDS} tabs={["disease"]} diseases={["MM"]} formData={{}} />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    unmount();
    render(
      <MemoryRouter>
        <FieldMappingLinksProvider value>
          <CustomPatientFields fields={FIELDS} tabs={["disease"]} diseases={["MM"]} formData={{}} />
        </FieldMappingLinksProvider>
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "Field concept mapping for tumor note" }))
      .toHaveAttribute("href", "/field-mappings?field=tumor_note");
  });
});
