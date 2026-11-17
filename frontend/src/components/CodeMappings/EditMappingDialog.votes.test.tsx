/**
 * The Votes section in the Edit Mapping dialog (#2678): asked for on a
 * wearable mapping only, closed until opened, and no part of a save.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import EditMappingDialog from "./EditMappingDialog";
import { emptyReference, type CodeMappingRow, type Reference } from "./codeMappingTypes";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPatch = vi.fn();
const mockDelete = vi.fn();
vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
    patch: (...args: unknown[]) => mockPatch(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));

const reference: Reference = {
  ...emptyReference,
  domains: [{ domain_id: "Measurement", label: "Measurement" }],
  source_code_systems_by_domain: { Measurement: [
    { vocabulary_id: "Apple", label: "HealthKit" }, { vocabulary_id: "LOINC", label: "LOINC" },
  ] },
  omop_tables: { Measurement: "measurement" },
  wearable_source_vocabularies: ["Apple", "Garmin", "HL7-PHR", "HealthConnect", "OpenWearables"],
};

const wearableRow: CodeMappingRow = {
  mapping_id: 41, domain_id: "Measurement", source_vocabulary_id: "Apple",
  source_code: "HKQuantityTypeIdentifierStepCount", source_code_description: "Step count",
  category: "Activity", measure_class: "Aggregate over interval", metric_key: "steps",
  destination_concept_id: 40758551, destination_concept_name: "Number of steps",
  destination_concept_code: "55423-8", destination_vocabulary_id: "LOINC",
  destination_concept_class_id: "Clinical Observation", destination_omop_table: "measurement",
  destination_domain_id: "Measurement", standard_concept: "S", status: "proposed", notes: "",
  origin: "import", origin_system: "hl7-phr-seed", suggest_strategy: "", umls_cui: "", created_by: "",
  occurrence_count: 0, destination_count: 1, has_mapping: true,
};
const labRow: CodeMappingRow = { ...wearableRow, mapping_id: 42, source_vocabulary_id: "LOINC", source_code: "8867-4" };
const VOTES = { tally: { yes: 1, no: 0, comments: 1 }, emails_included: true, votes: [{
  id: 9, choice: "yes", comment: "Right concept", confirmed_at: "2026-11-10T09:00:00+00:00",
  share_id: 1, share_title: "HL7 review", stale: false, voter_email: "voter@example.test",
}] };

function renderDialog(row: CodeMappingRow) {
  mockGet.mockImplementation((url: string) => Promise.resolve({ data:
    url === `/v1/code-mappings/${row.mapping_id}/votes/` ? VOTES
      : url === `/v1/code-mappings/${row.mapping_id}/` ? { ...row, destination_options: [] } : {},
  }));
  render(
    <EditMappingDialog open mode="edit" row={row} reference={reference} canApprove={false}
      strategies={{ umls: true, lexical: true, vectors: true }} rankingModel="anthropic"
      surface="code2concept" onClose={vi.fn()} onSaved={vi.fn()} onDeleted={vi.fn()} />,
  );
}
const votesRequests = () => mockGet.mock.calls.filter(([url]) => String(url).endsWith("/votes/"));

beforeEach(() => {
  [mockGet, mockPost, mockPatch, mockDelete].forEach((mock) => mock.mockReset());
  mockPost.mockResolvedValue({ data: {} });
  mockDelete.mockResolvedValue({ data: {} });
  mockPatch.mockImplementation((_url: string, payload: Record<string, unknown>) =>
    Promise.resolve({ data: { ...wearableRow, ...payload } }));
});

describe("EditMappingDialog votes", () => {
  it("shows a closed Votes section with the tally on a wearable mapping", async () => {
    renderDialog(wearableRow);
    const toggle = await screen.findByRole("button", { name: /^Votes/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveTextContent("1 yes · 1 comment");
    expect(screen.queryByText("voter@example.test")).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(screen.getByTestId("mapping-vote")).toHaveTextContent("voter@example.test");
    expect(screen.getByTestId("mapping-vote")).toHaveTextContent("Right concept");
    expect(votesRequests()).toEqual([["/v1/code-mappings/41/votes/"]]);
  });

  it("asks for no votes on a mapping outside the wearable vocabularies", async () => {
    renderDialog(labRow);
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/42/"));
    expect(votesRequests()).toEqual([]);
    expect(screen.queryByRole("button", { name: /^Votes/ })).not.toBeInTheDocument();
  });

  it("sends nothing about votes when the mapping is saved", async () => {
    renderDialog(wearableRow);
    await screen.findByRole("button", { name: /^Votes/ });
    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalled());
    const payload = mockPatch.mock.calls.at(-1)![1] as Record<string, unknown>;
    expect(Object.keys(payload).filter((key) => /vote/i.test(key))).toEqual([]);
  });
});
