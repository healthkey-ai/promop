import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { SynonymDialog } from "./SynonymDialog";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockDelete = vi.fn();

vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));

describe("SynonymDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders OMOP and custom synonyms", async () => {
    mockGet.mockResolvedValueOnce({
      data: [
        { id: 1, field_name: "hemoglobin_g_dl", synonym_text: "Hgb", source: "custom", created_by: "admin", created_at: "2024-01-01" },
        { id: null, field_name: "hemoglobin_g_dl", synonym_text: "Hemoglobin [Mass/volume]", source: "omop", created_by: null, created_at: null },
      ],
    });

    render(<SynonymDialog fieldName="hemoglobin_g_dl" onClose={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText("Hgb")).toBeInTheDocument();
    });
    expect(screen.getByText("Hemoglobin [Mass/volume]")).toBeInTheDocument();
    expect(screen.getByText("OMOP")).toBeInTheDocument();
    expect(screen.getByText("custom")).toBeInTheDocument();
  });

  it("shows empty state when no synonyms", async () => {
    mockGet.mockResolvedValueOnce({ data: [] });

    render(<SynonymDialog fieldName="hemoglobin_g_dl" onClose={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText("No OMOP synonyms. Assign a concept to see OMOP synonyms.")).toBeInTheDocument();
    });
    expect(screen.getByText("No custom synonyms yet.")).toBeInTheDocument();
  });

  it("adds a custom synonym", async () => {
    mockGet.mockResolvedValueOnce({ data: [] });
    mockPost.mockResolvedValueOnce({ data: { id: 1, synonym_text: "Hb", source: "custom" } });
    mockGet.mockResolvedValueOnce({
      data: [{ id: 1, field_name: "hemoglobin_g_dl", synonym_text: "Hb", source: "custom", created_by: "admin", created_at: "2024-01-01" }],
    });

    render(<SynonymDialog fieldName="hemoglobin_g_dl" onClose={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByPlaceholderText("Add a synonym...")).toBeInTheDocument();
    });

    const input = screen.getByPlaceholderText("Add a synonym...");
    fireEvent.change(input, { target: { value: "Hb" } });
    fireEvent.click(screen.getByText("Add"));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith(
        "/v1/field-mappings/hemoglobin_g_dl/synonyms/",
        { synonym_text: "Hb" }
      );
    });
  });

  it("deletes a custom synonym", async () => {
    mockGet.mockResolvedValueOnce({
      data: [
        { id: 5, field_name: "hemoglobin_g_dl", synonym_text: "Hgb", source: "custom", created_by: "admin", created_at: "2024-01-01" },
      ],
    });
    mockDelete.mockResolvedValueOnce({});
    mockGet.mockResolvedValueOnce({ data: [] });

    render(<SynonymDialog fieldName="hemoglobin_g_dl" onClose={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText("Hgb")).toBeInTheDocument();
    });

    const deleteButton = screen.getByTitle("Delete synonym");
    fireEvent.click(deleteButton);

    await waitFor(() => {
      expect(mockDelete).toHaveBeenCalledWith("/v1/field-synonyms/5/");
    });
  });

  it("calls onClose when clicking overlay", async () => {
    mockGet.mockResolvedValueOnce({ data: [] });
    const onClose = vi.fn();

    const { container } = render(<SynonymDialog fieldName="hemoglobin_g_dl" onClose={onClose} />);

    await waitFor(() => {
      expect(screen.getByText("Synonyms")).toBeInTheDocument();
    });

    // Click the overlay (the outermost fixed div)
    const overlay = container.querySelector(".fixed.inset-0");
    if (overlay) fireEvent.click(overlay);
    expect(onClose).toHaveBeenCalled();
  });
});
