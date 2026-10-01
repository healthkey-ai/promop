import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CodeMappingUploadDialog from "./CodeMappingUploadDialog";
import { formatUploadError } from "./codeMappingUploadError";

const mockPost = vi.fn();
vi.mock("@/api/axios", () => ({
  default: { post: (...args: unknown[]) => mockPost(...args) },
}));

describe("CodeMappingUploadDialog", () => {
  beforeEach(() => mockPost.mockReset());

  it("defaults provenance to the authenticated user email and uploads multipart CSV", async () => {
    const onUploaded = vi.fn();
    mockPost.mockResolvedValue({ data: {
      upload_id: 4, duplicate: false, total: 2, inserted: 1, updated: 1, unchanged: 0,
    } });
    render(<CodeMappingUploadDialog defaultProvenance="curator@example.com"
      onClose={vi.fn()} onUploaded={onUploaded} />);

    expect(screen.getByLabelText("Provenance")).toHaveValue("curator@example.com");
    expect(screen.getByText("Choose file")).toBeVisible();
    expect(screen.getByText(/destination concept ID/i)).toBeVisible();
    expect(screen.getByText(/blank defaults to Proposed/i)).toBeVisible();
    fireEvent.change(screen.getByLabelText("Source vocabulary"), { target: { value: "VendorLab" } });
    const file = new File(["source code,source description\nA01,Alpha\n"], "codes.csv", { type: "text/csv" });
    fireEvent.change(screen.getByLabelText("CSV file"), { target: { files: [file] } });
    expect(screen.getByText("codes.csv")).toBeVisible();
    await act(async () => {
      fireEvent.submit(screen.getByRole("dialog", { name: "Upload source codes" }));
    });

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    const [url, body] = mockPost.mock.calls[0] as [string, FormData];
    expect(url).toBe("/v1/code-mappings/upload/");
    expect(body.get("source_vocabulary_id")).toBe("VendorLab");
    expect(body.get("provenance")).toBe("curator@example.com");
    expect((body.get("file") as File).name).toBe("codes.csv");
    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith(expect.objectContaining({ total: 2 })));
  });

  it("formats server row validation errors", () => {
    const message = formatUploadError({ response: { data: {
        detail: "CSV validation failed on 1 row(s).",
        errors: [{ row: 3, detail: "Seen count must be a non-negative integer." }],
    } } });

    expect(message).toContain("Row 3: Seen count must be a non-negative integer.");
  });
});
