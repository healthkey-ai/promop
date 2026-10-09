import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { CustomPatientFields } from "./CustomPatientFields";

const mockedApi = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@/api/axios", () => ({ default: mockedApi }));

describe("CustomPatientFields", () => {
  beforeEach(() => {
    mockedApi.get.mockResolvedValue({ data: [{
      id: 1, field_name: "tumor_note", display_name: "Tumor note", tab: "disease",
      field_type: "text", mode: "editable", concept_id: 1, concept_name: "Tumor",
      vocabulary_id: "SNOMED", concept_code: "1", omop_table: "Observation", unit: "",
    }] });
  });

  it("shows approved fields to all users and only exposes Add field to mapping admins", async () => {
    const { rerender } = render(<CustomPatientFields tab="disease" formData={{ custom_fields: { tumor_note: "Stable" } }} />);
    expect(await screen.findByText("Tumor note")).toBeInTheDocument();
    expect(screen.getByText("Stable")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add field/i })).not.toBeInTheDocument();
    rerender(<CustomPatientFields tab="disease" formData={{ custom_fields: { tumor_note: "Stable" } }} canManage />);
    expect(screen.getByRole("button", { name: /add field/i })).toBeInTheDocument();
  });

  it("requires a mode-specific formula before adding a computed field", async () => {
    render(<CustomPatientFields tab="disease" formData={{}} canManage />);
    fireEvent.click(await screen.findByRole("button", { name: /add field/i }));
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Double weight" } });
    fireEvent.click(screen.getByLabelText("Computed"));
    expect(screen.getByLabelText("Formula")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Add field" }).at(-1)).toBeDisabled();
    await waitFor(() => expect(mockedApi.post).not.toHaveBeenCalled());
  });
});
