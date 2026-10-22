import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import FieldMappingPage from "./FieldMappingPage";

const { mockGet, mockDelete } = vi.hoisted(() => ({ mockGet: vi.fn(), mockDelete: vi.fn() }));

vi.mock("@/api/axios", () => ({
  default: { get: mockGet, delete: mockDelete },
}));
vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ currentUser: { is_staff: true } }),
}));

it("keeps the allowed value uncoded when the mapping is reopened after code removal", async () => {
  let codes = [{ id: 51, code: "55561003", vocabulary_id: "SNOMED", display: "Active", is_primary: true }];
  const choice = () => ({ id: 7, field_name: "disease_activity", display: "Active", sort_order: 2, codes });
  mockGet.mockImplementation(async (url: string) => {
    if (url === "/v1/field-mappings/") return { data: [{
      field_name: "disease_activity", field_type: "text", category: "editable", tab: "disease",
      provenance: null, mappable: true, locked_table: null, suggestion: null,
      formula: null, explanation: null, choices: [choice()],
      mapping: { id: 3, concept_id: 123, concept_code: "activity-test", concept_name: "Activity",
        vocabulary_id: "LOCAL", status: "approved", omop_table: "observation", unit: "", notes: "" },
    }] };
    if (url === "/v1/field-choices/?field_name=disease_activity") return { data: [choice()] };
    if (url.startsWith("/v1/field-synonyms/batch/")) return { data: {} };
    throw new Error(`Unexpected GET ${url}`);
  });
  mockDelete.mockImplementation(async (url: string, config: { params: { code_id: number } }) => {
    expect(url).toBe("/v1/field-choices/7/codes/");
    expect(config.params.code_id).toBe(51);
    codes = [];
    return { data: null };
  });

  render(<MemoryRouter><FieldMappingPage /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /^Disease/ }));
  fireEvent.click(await screen.findByText("activity-test"));
  expect(within(screen.getByRole("dialog")).getByText("SNOMED:55561003")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Edit choices" }));
  fireEvent.click(await screen.findByRole("button", { name: "Remove code SNOMED:55561003 from Active" }));
  await waitFor(() => expect(screen.queryByText("SNOMED:55561003")).not.toBeInTheDocument());
  expect(screen.getByText("Active")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Done" }));

  fireEvent.click(await screen.findByText("activity-test"));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByRole("cell", { name: "Active" })).toBeInTheDocument();
  expect(within(dialog).getByRole("cell", { name: "no codes" })).toBeInTheDocument();
  expect(within(dialog).queryByText("SNOMED:55561003")).not.toBeInTheDocument();
  expect(mockDelete).toHaveBeenCalledTimes(1);
});
