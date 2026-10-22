import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AdvanceDirectives from "./AdvanceDirectives";

const mockGet = vi.fn();
const mockPost = vi.fn();
vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
  },
}));

const user = {
  person_id: 4242,
  email: "patient@example.com",
} as unknown as Parameters<typeof AdvanceDirectives>[0]["user"];

describe("AdvanceDirectives", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();
    mockGet.mockResolvedValue({ data: { results: [] } });
    mockPost.mockResolvedValue({ data: { id: 7 } });
  });

  function chooseFile(name = "directive.pdf") {
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [
      new File(["%PDF-1.4"], name, { type: "application/pdf" }),
    ] } });
  }

  // The shared api instance declares 'Content-Type: application/json' as a
  // default, and axios reads that header before deciding how to encode a body.
  // For FormData it then stringifies the entries and every File collapses to
  // {} -- so the FileField on the documents viewset saw no upload and the row
  // was rejected, reported only as "Failed to upload advance directive."
  // Clearing the header for this request keeps the body multipart.
  it("clears the instance Content-Type so the document stays multipart", async () => {
    render(<AdvanceDirectives user={user} />);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    await act(async () => { chooseFile(); });

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    const [url, body, config] = mockPost.mock.calls[0] as [
      string, FormData, { headers?: Record<string, string | undefined> } | undefined,
    ];
    expect(url).toBe("/v1/documents/");
    expect(body).toBeInstanceOf(FormData);
    expect(body.get("doc_type")).toBe("ADVANCE_DIRECTIVE");
    expect(body.get("person")).toBe("4242");
    expect(body.get("file")).toBeInstanceOf(File);
    expect((body.get("file") as File).name).toBe("directive.pdf");
    expect(config?.headers).toHaveProperty("Content-Type");
    expect(config?.headers?.["Content-Type"]).toBeUndefined();
  });

  it("reports a failed upload", async () => {
    mockPost.mockRejectedValue(new Error("boom"));
    render(<AdvanceDirectives user={user} />);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    await act(async () => { chooseFile(); });

    expect(await screen.findByText(/Failed to upload advance directive/i)).toBeVisible();
  });
});
