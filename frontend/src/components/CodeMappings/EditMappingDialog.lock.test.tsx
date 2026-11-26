/**
 * The edit lock is held only while the dialog is open on a mapping: every way
 * out of the dialog releases it, not just the Close button.
 */
import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import EditMappingDialog from "./EditMappingDialog";
import { emptyReference, type CodeMappingRow } from "./codeMappingTypes";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockDelete = vi.fn();
vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
    patch: vi.fn(),
    delete: (...args: unknown[]) => mockDelete(...args),
    defaults: { baseURL: "/api" },
  },
}));

const row: CodeMappingRow = {
  mapping_id: 7, domain_id: "Measurement", source_vocabulary_id: "VendorLab", source_code: "A01",
  source_code_description: "Albumin", destination_concept_id: null, destination_concept_name: "",
  destination_concept_code: "", destination_vocabulary_id: "", destination_concept_class_id: "",
  destination_omop_table: "measurement", destination_domain_id: "Measurement", standard_concept: "",
  status: "proposed", notes: "", origin: "curator", origin_system: "curator", suggest_strategy: "",
  umls_cui: "", created_by: "", occurrence_count: 0, destination_count: 0, has_mapping: true,
};

const LOCK = "/v1/code-mappings/7/lock/";

function dialog(props: { open: boolean; row: CodeMappingRow }) {
  return (
    <EditMappingDialog open={props.open} mode="edit" row={props.row} reference={emptyReference} canApprove
      currentUserId={1} strategies={{ umls: true, lexical: true, vectors: true }}
      rankingModel="anthropic" surface="code2concept" onClose={vi.fn()} onSaved={vi.fn()}
      onDeleted={vi.fn()} />
  );
}

describe("EditMappingDialog edit lock", () => {
  beforeEach(() => {
    mockGet.mockReset().mockResolvedValue({ data: {} });
    mockPost.mockReset().mockResolvedValue({ data: {} });
    mockDelete.mockReset().mockResolvedValue({ data: {} });
  });

  it("takes the lock on open and releases it when the dialog closes by any route", async () => {
    const view = render(dialog({ open: true, row }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith(LOCK));
    expect(mockDelete).not.toHaveBeenCalled();
    // The parent closing the dialog (Escape, backdrop, a save) is not the Close button.
    view.rerender(dialog({ open: false, row }));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith(LOCK));
  });

  it("releases the lock when the page unmounts", async () => {
    const view = render(dialog({ open: true, row }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith(LOCK));
    view.unmount();
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith(LOCK));
  });

  it("releases one mapping's lock when another row is opened", async () => {
    const view = render(dialog({ open: true, row }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith(LOCK));
    view.rerender(dialog({ open: true, row: { ...row, mapping_id: 8 } }));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith(LOCK));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/8/lock/"));
  });

  it("does not try to release a lock another user holds", async () => {
    mockPost.mockRejectedValue({ response: { status: 423, data: { locked_by: "Ada" } } });
    const view = render(dialog({ open: true, row }));
    await waitFor(() => expect(mockPost).toHaveBeenCalled());
    view.unmount();
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it("releases a lock granted after the dialog already closed", async () => {
    let grant: (value: unknown) => void = () => {};
    mockPost.mockReturnValue(new Promise((resolve) => { grant = resolve; }));
    const view = render(dialog({ open: true, row }));
    view.unmount();
    expect(mockDelete).not.toHaveBeenCalled();
    grant({ data: {} });
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith(LOCK));
  });
});
