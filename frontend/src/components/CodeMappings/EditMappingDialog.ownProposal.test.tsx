/**
 * The two-person rule in the Edit Mapping dialog (#2726): the curator who
 * proposed a mapping's destination is not offered Approved. The server refuses
 * the save either way; the dialog says so before the click.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import EditMappingDialog from "./EditMappingDialog";
import { emptyReference, isOwnProposal, type CodeMappingRow } from "./codeMappingTypes";

const mockGet = vi.fn();
vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}));

const row: CodeMappingRow = {
  mapping_id: 7, domain_id: "Measurement", source_vocabulary_id: "VendorLab", source_code: "A01",
  source_code_description: "Albumin", destination_concept_id: 3024561, destination_concept_name: "Albumin",
  destination_concept_code: "1751-7", destination_vocabulary_id: "LOINC",
  destination_concept_class_id: "Lab Test", destination_omop_table: "measurement",
  destination_domain_id: "Measurement", standard_concept: "S", status: "proposed", notes: "",
  origin: "curator", origin_system: "curator", suggest_strategy: "", umls_cui: "", created_by: "",
  occurrence_count: 0, destination_count: 1, has_mapping: true, proposed_by_id: 11,
};

function renderDialog(currentUserId: number | null, mapping: CodeMappingRow = row, separateApprover?: boolean) {
  mockGet.mockImplementation((url: string) => Promise.resolve({
    data: url === `/v1/code-mappings/${mapping.mapping_id}/` ? { ...mapping, destination_options: [] } : {},
  }));
  render(
    <EditMappingDialog open mode="edit" row={mapping} reference={{ ...emptyReference, separate_approver: separateApprover }} canApprove
      currentUserId={currentUserId} strategies={{ umls: true, lexical: true, vectors: true }}
      rankingModel="anthropic" surface="code2concept" onClose={vi.fn()} onSaved={vi.fn()}
      onDeleted={vi.fn()} />,
  );
}

describe("isOwnProposal", () => {
  it("is true only for the recorded proposer", () => {
    expect(isOwnProposal({ proposed_by_id: 11 }, 11)).toBe(true);
    expect(isOwnProposal({ proposed_by_id: 11 }, "11")).toBe(true);
    expect(isOwnProposal({ proposed_by_id: 11 }, 12)).toBe(false);
    expect(isOwnProposal({ proposed_by_id: null }, 11)).toBe(false);
    expect(isOwnProposal({}, 11)).toBe(false);
    expect(isOwnProposal(null, 11)).toBe(false);
    expect(isOwnProposal({ proposed_by_id: 11 }, null)).toBe(false);
  });
});

describe("EditMappingDialog approval by the proposer", () => {
  beforeEach(() => mockGet.mockReset());

  it("disables Approved for the curator who proposed the destination", async () => {
    renderDialog(11);
    const option = await screen.findByRole("option", { name: "Approved (you proposed this)" });
    expect(option).toBeDisabled();
    expect(option).toHaveAttribute("title", expect.stringContaining("another approver"));
  });

  it("offers Approved to a different approver", async () => {
    renderDialog(12);
    const option = await screen.findByRole("option", { name: "Approved" });
    await waitFor(() => expect(option).toBeEnabled());
  });

  it("offers Approved when the rule is off and no proposer is reported", async () => {
    renderDialog(11, { ...row, proposed_by_id: null });
    const option = await screen.findByRole("option", { name: "Approved" });
    await waitFor(() => expect(option).toBeEnabled());
  });

  it("disables Approved for a mapping with no destination yet when the rule is on, since picking one proposes it", async () => {
    renderDialog(12, { ...row, destination_concept_id: null, destination_concept_name: "", proposed_by_id: null }, true);
    // No destination: the standing "set destination first" label wins.
    expect(await screen.findByRole("option", { name: "Approved (set destination first)" })).toBeDisabled();
  });

  it("keeps Approved open for another approver when the destination is unchanged", async () => {
    renderDialog(12, row, true);
    const option = await screen.findByRole("option", { name: "Approved" });
    await waitFor(() => expect(option).toBeEnabled());
  });
});
