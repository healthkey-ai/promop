import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { FormulaEditDialog } from "./FormulaEditDialog";

const mockPost = vi.fn();
const mockPatch = vi.fn();
const mockDelete = vi.fn();

vi.mock("@/api/axios", () => ({
  default: {
    get: vi.fn().mockResolvedValue({ data: {} }),
    post: (...args: unknown[]) => mockPost(...args),
    patch: (...args: unknown[]) => mockPatch(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));

describe("FormulaEditDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPost.mockResolvedValue({ data: {} });
    mockPatch.mockResolvedValue({ data: {} });
    mockDelete.mockResolvedValue({ data: {} });
  });

  it("renders with field name and formula", () => {
    render(
      <FormulaEditDialog
        fieldName="bmi"
        fieldType="float"
        existingFormula={{ id: 1, expression: "weight / (height / 100) ^ 2", is_active: false }}
        onClose={vi.fn()}
      />
    );
    expect(screen.getByText("Edit Formula")).toBeInTheDocument();
    expect(screen.getByDisplayValue("weight / (height / 100) ^ 2")).toBeInTheDocument();
  });

  it("renders Add Formula title for new formula", () => {
    render(
      <FormulaEditDialog
        fieldName="bmi"
        fieldType="float"
        existingFormula={null}
        onClose={vi.fn()}
      />
    );
    expect(screen.getByText("Add Formula")).toBeInTheDocument();
  });

  it("calls PATCH API to save existing formula", async () => {
    render(
      <FormulaEditDialog
        fieldName="bmi"
        fieldType="float"
        existingFormula={{ id: 1, expression: "old formula", is_active: false }}
        onClose={vi.fn()}
      />
    );
    const textarea = screen.getByDisplayValue("old formula");
    fireEvent.change(textarea, { target: { value: "new formula" } });
    fireEvent.click(screen.getByText("Update"));
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith("/v1/field-formulas/1/", expect.objectContaining({
        formula: "new formula",
      }));
    });
  });

  it("calls POST API to create new formula", async () => {
    render(
      <FormulaEditDialog
        fieldName="test_field"
        fieldType="float"
        existingFormula={null}
        onClose={vi.fn()}
      />
    );
    const textarea = screen.getByPlaceholderText(/e\.g\. @not/);
    fireEvent.change(textarea, { target: { value: "@not(hiv_status)" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith("/v1/field-formulas/", expect.objectContaining({
        field_name: "test_field",
        formula: "@not(hiv_status)",
      }));
    });
  });

  it("shows syntax reference", () => {
    render(
      <FormulaEditDialog
        fieldName="bmi"
        fieldType="float"
        existingFormula={null}
        onClose={vi.fn()}
      />
    );
    expect(screen.getByText("Supported syntax:")).toBeInTheDocument();
  });

  it("tests a formula against a patient record", async () => {
    render(<FormulaEditDialog fieldName="bmi" fieldType="float" existingFormula={null} onClose={vi.fn()} />);
    fireEvent.change(screen.getByPlaceholderText(/e\.g\. @not/), { target: { value: "weight / 2" } });
    fireEvent.change(screen.getByPlaceholderText("Patient ID"), { target: { value: "42" } });
    fireEvent.click(screen.getByText("Test"));
    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith("/v1/field-formulas/test/", {
        formula: "weight / 2", person_id: 42,
      });
    });
  });
});
