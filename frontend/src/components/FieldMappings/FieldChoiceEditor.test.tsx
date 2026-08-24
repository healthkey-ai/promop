import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { FieldChoiceEditor } from "./FieldChoiceEditor";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockDelete = vi.fn();

vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));

const MOCK_CHOICES = [
  {
    id: 1,
    field_name: "disease",
    display: "Follicular Lymphoma",
    sort_order: 0,
    codes: [
      { id: 1, code: "307618003", vocabulary_id: "SNOMED", display: "Follicular lymphoma", is_primary: true },
    ],
    created_by: null,
    created_at: "2024-01-01",
  },
  {
    id: 2,
    field_name: "disease",
    display: "Multiple Myeloma",
    sort_order: 1,
    codes: [],
    created_by: null,
    created_at: "2024-01-01",
  },
];

describe("FieldChoiceEditor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGet.mockResolvedValue({ data: MOCK_CHOICES });
    mockPost.mockResolvedValue({ data: { id: 3, field_name: "disease", display: "New Disease" } });
    mockDelete.mockResolvedValue({ data: {} });
  });

  it("renders existing choices", async () => {
    render(<FieldChoiceEditor fieldName="disease" onClose={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("Follicular Lymphoma")).toBeInTheDocument();
    });
    expect(screen.getByText("Multiple Myeloma")).toBeInTheDocument();
    expect(screen.getByText("SNOMED:307618003")).toBeInTheDocument();
  });

  it("calls API to add choice", async () => {
    render(<FieldChoiceEditor fieldName="disease" onClose={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("Follicular Lymphoma")).toBeInTheDocument();
    });
    const input = screen.getByPlaceholderText("New choice display name...");
    fireEvent.change(input, { target: { value: "Breast Cancer" } });
    fireEvent.click(screen.getByText("Add"));
    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith("/v1/field-choices/", expect.objectContaining({
        field_name: "disease",
        display: "Breast Cancer",
      }));
    });
  });

  it("calls API to delete choice", async () => {
    // Mock window.confirm
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<FieldChoiceEditor fieldName="disease" onClose={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("Follicular Lymphoma")).toBeInTheDocument();
    });
    const deleteButtons = screen.getAllByTitle("Delete choice");
    fireEvent.click(deleteButtons[0]);
    await waitFor(() => {
      expect(mockDelete).toHaveBeenCalledWith("/v1/field-choices/1/");
    });
  });
});
