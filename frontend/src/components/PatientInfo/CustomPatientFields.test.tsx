import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { CustomPatientFields } from "./CustomPatientFields";
import { FieldMappingLinksProvider } from "./fieldMappingLinks";
import { ProvisionalContext } from "./ProvisionalContext";
import type { CustomPatientField } from "@/hooks/useCustomPatientFields";
import type { FieldDescriptors } from "@/hooks/useWritableFields";

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
  field(5, "double_weight", "general", [], { field_type: "number", mode: "computed" }),
];

const writable = { kind: "direct", writable: true, target: "patient_record" } as const;
const DESCRIPTORS: FieldDescriptors = {
  tumor_note: writable, her2_note: writable, frailty_index: writable, transfusions: writable,
  double_weight: { kind: "computed", writable: false, reason: "Computed from its formula; not editable here." },
};

/** The box of one added field, found through its label's wrapper. */
const box = (label: string) => {
  const wrapper = screen.getByText(label).closest("div.space-y-1\\.5") as HTMLElement;
  return wrapper.querySelector("input") as HTMLInputElement;
};

describe("CustomPatientFields", () => {
  it("shows the added fields of the tab that are part of this record, with their values", () => {
    render(<CustomPatientFields fields={FIELDS} tabs={["disease"]} diseases={["MM"]} formData={{ custom_fields: { tumor_note: "Stable" } }} />);
    expect(screen.getByRole("region", { name: "Additional fields" })).toBeInTheDocument();
    expect(box("tumor note")).toHaveValue("Stable");
    // Another disease's field, and another tab's, are not this tab's.
    expect(screen.queryByText("her2 note")).not.toBeInTheDocument();
    expect(screen.queryByText(/frailty index/)).not.toBeInTheDocument();
  });

  it("says whose a disease field is when the record has more than one cancer, and not otherwise", () => {
    const fields = [field(1, "tumor_note", "disease", ["MM"]), field(2, "psa_note", "disease", ["PC"]),
      field(3, "shared_note", "disease")];
    const labels = { MM: "Multiple Myeloma", PC: "Prostate Cancer" };
    const { rerender } = render(<CustomPatientFields fields={fields} tabs={["disease"]} diseases={["MM", "PC"]}
      diseaseLabels={labels} formData={{}} />);
    expect(screen.getByTestId("scope-tumor_note")).toHaveTextContent("Multiple Myeloma");
    expect(screen.getByTestId("scope-psa_note")).toHaveTextContent("Prostate Cancer");
    expect(screen.queryByTestId("scope-shared_note")).not.toBeInTheDocument();
    rerender(<CustomPatientFields fields={fields} tabs={["disease"]} diseases={["MM"]} diseaseLabels={labels} formData={{}} />);
    expect(screen.queryByTestId("scope-tumor_note")).not.toBeInTheDocument();
    expect(screen.queryByText("psa note")).not.toBeInTheDocument();
  });

  it("shows a base field to every record and a blood field on the labs tab, with the unit in the label", () => {
    render(<CustomPatientFields fields={FIELDS} tabs={["labs", "blood"]} diseases={[]} formData={{ custom_fields: { frailty_index: 4 } }} />);
    expect(box("frailty index (points)")).toHaveValue(4);
    expect(box("transfusions")).toHaveValue("");
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

/** Editing (#2669): a field takes a value when the server says the caller may write it. */
describe("CustomPatientFields editing", () => {
  it("hands an edit back by field name", () => {
    const onChange = vi.fn();
    render(<CustomPatientFields fields={FIELDS} tabs={["disease"]} diseases={["MM"]} formData={{ custom_fields: { tumor_note: "Stable" } }} descriptors={DESCRIPTORS} onChange={onChange} />);
    expect(box("tumor note")).toBeEnabled();
    fireEvent.change(box("tumor note"), { target: { value: "Progressing" } });
    expect(onChange).toHaveBeenCalledWith("tumor_note", "Progressing");
  });

  it("hands back a number as a number, and null for an emptied box", () => {
    const onChange = vi.fn();
    render(<CustomPatientFields fields={FIELDS} tabs={["labs"]} diseases={[]} formData={{ custom_fields: { frailty_index: 4 } }} descriptors={DESCRIPTORS} onChange={onChange} />);
    fireEvent.change(box("frailty index (points)"), { target: { value: "5.5" } });
    expect(onChange).toHaveBeenLastCalledWith("frailty_index", 5.5);
    fireEvent.change(box("frailty index (points)"), { target: { value: "" } });
    expect(onChange).toHaveBeenLastCalledWith("frailty_index", null);
  });

  it("clears a text value to null", () => {
    const onChange = vi.fn();
    render(<CustomPatientFields fields={FIELDS} tabs={["disease"]} diseases={["MM"]} formData={{ custom_fields: { tumor_note: "Stable" } }} descriptors={DESCRIPTORS} onChange={onChange} />);
    fireEvent.change(box("tumor note"), { target: { value: "" } });
    expect(onChange).toHaveBeenLastCalledWith("tumor_note", null);
  });

  it("keeps a computed field read-only and says why", () => {
    render(<CustomPatientFields fields={FIELDS} tabs={["general"]} diseases={[]} formData={{ custom_fields: { double_weight: 140 } }} descriptors={DESCRIPTORS} onChange={vi.fn()} />);
    expect(box("double weight")).toBeDisabled();
    expect(box("double weight")).toHaveValue(140);
    expect(screen.getByTestId("reason-double_weight")).toHaveTextContent("Computed from its formula");
  });

  it("is read-only for a caller the server does not let write, with the server's reason", () => {
    const readOnly: FieldDescriptors = { tumor_note: {
      kind: "direct", writable: false, read_only_for_caller: true,
      reason: "You have read-only access to this patient record.",
    } };
    const onChange = vi.fn();
    render(<CustomPatientFields fields={FIELDS} tabs={["disease"]} diseases={["MM"]} formData={{}} descriptors={readOnly} onChange={onChange} />);
    expect(box("tumor note")).toBeDisabled();
    expect(screen.getByTestId("reason-tumor_note")).toHaveTextContent("read-only access");
  });

  it("fails closed: no descriptor, or no handler, is no input", () => {
    const { unmount } = render(<CustomPatientFields fields={FIELDS} tabs={["disease"]} diseases={["MM"]} formData={{}} onChange={vi.fn()} />);
    expect(box("tumor note")).toBeDisabled();
    unmount();
    render(<CustomPatientFields fields={FIELDS} tabs={["disease"]} diseases={["MM"]} formData={{}} descriptors={DESCRIPTORS} />);
    expect(box("tumor note")).toBeDisabled();
  });

  it("marks a proposed value Provisional with its confidence, by the name the server reports", () => {
    render(
      <ProvisionalContext.Provider value={{ marks: { "custom_fields.tumor_note": 82 }, patientMode: true }}>
        <CustomPatientFields fields={FIELDS} tabs={["disease"]} diseases={["MM"]}
          formData={{ custom_fields: { tumor_note: "Stable" } }} descriptors={DESCRIPTORS} onChange={vi.fn()} />
      </ProvisionalContext.Provider>,
    );
    expect(screen.getByTestId("provisional-custom_fields.tumor_note")).toHaveTextContent("Provisional 82%");
    expect(box("tumor note")).toHaveValue("Stable");
  });

  it("marks nothing for a value that is not a proposal, and a bare column name is not its mark", () => {
    render(
      <ProvisionalContext.Provider value={{ marks: { tumor_note: 82 }, patientMode: false }}>
        <CustomPatientFields fields={FIELDS} tabs={["disease"]} diseases={["MM"]}
          formData={{ custom_fields: { tumor_note: "Stable" } }} descriptors={DESCRIPTORS} onChange={vi.fn()} />
      </ProvisionalContext.Provider>,
    );
    expect(screen.queryByText(/Provisional/)).not.toBeInTheDocument();
  });
});
