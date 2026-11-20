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

  // The mock above replaces the api instance, so it cannot see the instance
  // default that broke this: real axios reads a JSON Content-Type and, for a
  // FormData body, stringifies the entries, dropping the File to {}. Assert on
  // the arguments the component passes, so the regression is visible in the
  // mock: the per-request header must clear Content-Type.
  it("clears the instance Content-Type so the CSV stays multipart", async () => {
    const onUploaded = vi.fn();
    mockPost.mockResolvedValue({ data: {
      upload_id: 5, duplicate: false, total: 1, inserted: 1, updated: 0, unchanged: 0,
    } });
    render(<CodeMappingUploadDialog defaultProvenance="curator@example.com"
      onClose={vi.fn()} onUploaded={onUploaded} />);

    fireEvent.change(screen.getByLabelText("Source vocabulary"), { target: { value: "CB" } });
    fireEvent.change(screen.getByLabelText("CSV file"), { target: { files: [
      new File(["source code,source description\ntherapy:br,Break\n"], "cb.csv", { type: "text/csv" }),
    ] } });
    await act(async () => {
      fireEvent.submit(screen.getByRole("dialog", { name: "Upload source codes" }));
    });

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    const [url, body, config] = mockPost.mock.calls[0] as [
      string, FormData, { headers?: Record<string, string | undefined> } | undefined,
    ];
    expect(url).toBe("/v1/code-mappings/upload/");
    expect(body).toBeInstanceOf(FormData);
    // The instance default is application/json; leaving it in place is what
    // made axios stringify the entries and lose the file.
    expect(config?.headers).toHaveProperty("Content-Type");
    expect(config?.headers?.["Content-Type"]).toBeUndefined();
  });

  // axios is mocked here, so the encoding itself is not exercised. Assert on
  // real axios with the real instance default: a FormData body must survive as
  // FormData with the File intact, and not be stringified into JSON.
  it("axios keeps a FormData file intact once Content-Type is not json", async () => {
    const { default: realAxios } = await import("axios");
    const fd = new FormData();
    fd.append("file", new File(["a,b\n1,2\n"], "cb.csv", { type: "text/csv" }));

    const fixed = realAxios.create({ headers: { "Content-Type": "application/json" } });
    const capture = (instance: typeof realAxios) => {
      let body: unknown;
      instance.defaults.adapter = (config) => {
        body = config.data;
        return Promise.resolve({ data: {}, status: 200, statusText: "OK", headers: {}, config });
      };
      return instance.post("/x", fd, { headers: { "Content-Type": undefined } })
        .then(() => body);
    };

    const fixedBody = await capture(fixed) as FormData;
    expect(fixedBody).toBeInstanceOf(FormData);
    expect(fixedBody.get("file")).toBeInstanceOf(File);
    expect((fixedBody.get("file") as File).name).toBe("cb.csv");
    // Documents the original defect: the same body under the json default is
    // flattened, and the file is gone.
    const brokenInstance = realAxios.create({ headers: { "Content-Type": "application/json" } });
    let brokenBody: unknown;
    brokenInstance.defaults.adapter = (config) => {
      brokenBody = config.data;
      return Promise.resolve({ data: {}, status: 200, statusText: "OK", headers: {}, config });
    };
    await brokenInstance.post("/x", fd).catch(() => undefined);
    expect(brokenBody).not.toBeInstanceOf(FormData);
    expect(String(brokenBody)).toContain('"file":{}');
  });
});
