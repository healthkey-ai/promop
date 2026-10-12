import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";
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

  afterEach(() => vi.restoreAllMocks());

  it("removes only the selected code and keeps the allowed value and other codes", async () => {
    const otherCode = { id: 9, code: "C82", vocabulary_id: "ICD10CM", display: "Follicular lymphoma", is_primary: false };
    const choices = [{ ...MOCK_CHOICES[0], codes: [...MOCK_CHOICES[0].codes, otherCode] }, MOCK_CHOICES[1]];
    mockGet.mockResolvedValueOnce({ data: choices }).mockResolvedValue({
      data: [{ ...choices[0], codes: [otherCode] }, choices[1]],
    });
    render(<FieldChoiceEditor fieldName="disease" onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove code SNOMED:307618003 from Follicular Lymphoma" }));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith(
      "/v1/field-choices/1/codes/", { params: { code_id: 1 } },
    ));
    await waitFor(() => expect(screen.queryByText("SNOMED:307618003")).not.toBeInTheDocument());
    expect(screen.getByText("Follicular Lymphoma")).toBeInTheDocument();
    expect(screen.getByText("ICD10CM:C82")).toBeInTheDocument();
    expect(screen.getByText("Multiple Myeloma")).toBeInTheDocument();
    expect(mockDelete).toHaveBeenCalledTimes(1);
  });

  it("retains the choice as uncoded when its last code is removed", async () => {
    mockGet.mockResolvedValueOnce({ data: MOCK_CHOICES }).mockResolvedValue({
      data: [{ ...MOCK_CHOICES[0], codes: [] }, MOCK_CHOICES[1]],
    });
    render(<FieldChoiceEditor fieldName="disease" onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove code SNOMED:307618003 from Follicular Lymphoma" }));
    await waitFor(() => expect(screen.queryByText("SNOMED:307618003")).not.toBeInTheDocument());
    expect(screen.getByText("Follicular Lymphoma")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Add code to/ })).toHaveLength(2);
  });

  it("keeps the code visible and reports a failed removal", async () => {
    mockDelete.mockRejectedValueOnce(new Error("Request failed"));
    render(<FieldChoiceEditor fieldName="disease" onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove code SNOMED:307618003 from Follicular Lymphoma" }));
    expect(await screen.findByText("Failed to remove code.")).toBeInTheDocument();
    expect(screen.getByText("SNOMED:307618003")).toBeInTheDocument();
    expect(screen.getByText("Follicular Lymphoma")).toBeInTheDocument();
  });

  it("prevents duplicate code removals while the request is pending", async () => {
    let resolveDelete!: (value: unknown) => void;
    mockDelete.mockReturnValueOnce(new Promise((resolve) => { resolveDelete = resolve; }));
    render(<FieldChoiceEditor fieldName="disease" onClose={vi.fn()} />);
    const remove = await screen.findByRole("button", { name: "Remove code SNOMED:307618003 from Follicular Lymphoma" });
    fireEvent.click(remove);
    expect(remove).toBeDisabled();
    fireEvent.click(remove);
    expect(mockDelete).toHaveBeenCalledTimes(1);
    resolveDelete({ data: {} });
    await waitFor(() => expect(screen.getByRole("button", { name: "Remove code SNOMED:307618003 from Follicular Lymphoma" })).toBeEnabled());
  });

  it("cancels whole-choice deletion without changing the choice or its codes", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<FieldChoiceEditor fieldName="disease" onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete choice Follicular Lymphoma" }));
    expect(confirm).toHaveBeenCalledWith('Delete the entire choice "Follicular Lymphoma" and all of its codes?');
    expect(mockDelete).not.toHaveBeenCalled();
    expect(screen.getByText("SNOMED:307618003")).toBeInTheDocument();
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
