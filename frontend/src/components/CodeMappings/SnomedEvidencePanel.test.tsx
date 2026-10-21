import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SnomedEvidencePanel from "./SnomedEvidencePanel";
import type { SnomedAttributesPayload } from "./snomed";

const { get, put } = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));
vi.mock("@/api/axios", () => ({ default: { get, put } }));

const pneumonia = { concept_id: 255848, concept_name: "Pneumonia", concept_code: "233604007", vocabulary_id: "SNOMED",
  domain_id: "Condition", concept_class_id: "Disorder", standard_concept: "S" };

const payload = (overrides: Partial<SnomedAttributesPayload> = {}): SnomedAttributesPayload => ({
  mapping_id: 7,
  applies: true,
  snomed_attributes: {},
  source: {
    references: [{ concept_id: 4112343, concept_name: "Fracture of femur", concept_code: "71620000", hierarchy: "Disorder" }],
    reference_basis: "maps_to",
    hierarchy: { value: "Disorder", basis: "reference", evidence: "Fracture of femur (disorder)" },
    laterality: { value: "Right", basis: "text", evidence: "description = R FEMUR FX" },
    attributes: [
      { relationship_id: "Has finding site", label: "Finding site", values: ["Bone structure of femur"] },
      { relationship_id: "Has asso morph", label: "Associated morphology", values: ["Fracture"] },
    ],
  },
  hierarchies: { Disorder: "disorder", "Clinical Finding": "finding", Procedure: "procedure" },
  lateralities: ["Left", "Right", "Right and left"],
  ...overrides,
});

beforeEach(() => {
  vi.resetAllMocks();
  get.mockImplementation((url: string) => Promise.resolve({ data: url.includes("/concepts/search/") ? { results: [pneumonia] } : payload() }));
  put.mockImplementation((_url: string, body: { snomed_attributes: SnomedAttributesPayload["snomed_attributes"] }) =>
    Promise.resolve({ data: payload({ snomed_attributes: body.snomed_attributes }) }));
});

describe("SnomedEvidencePanel", () => {
  it("shows the reference concept with its basis, hierarchy, laterality and attributes", async () => {
    render(<SnomedEvidencePanel mappingId={7} canEdit />);
    expect(await screen.findByRole("region", { name: "SNOMED evidence" })).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/v1/code-mappings/7/snomed-attributes/");
    expect(screen.getByTestId("snomed-reference")).toHaveTextContent("Fracture of femur SNOMED:71620000");
    expect(screen.getByTestId("snomed-reference-basis")).toHaveTextContent("Maps to");
    expect(screen.getByTestId("snomed-hierarchy")).toHaveTextContent("Disorder (disorder)");
    expect(screen.getByTestId("snomed-laterality")).toHaveTextContent("Right");
    expect(screen.getByTestId("snomed-laterality-why")).toHaveTextContent("Named in the source text: description = R FEMUR FX");
    expect(screen.getByRole("list", { name: "Reference attributes" })).toHaveTextContent("Finding site: Bone structure of femur");
    expect(screen.getByText(/The source means at least the reference concept/)).toBeInTheDocument();
  });

  it("sets a reference, a hierarchy and no side, reports them unsaved, and PUTs them", async () => {
    const onSettingsChange = vi.fn();
    render(<SnomedEvidencePanel mappingId={7} canEdit onSettingsChange={onSettingsChange} />);
    await screen.findByRole("region", { name: "SNOMED evidence" });
    expect(onSettingsChange).toHaveBeenLastCalledWith({});
    expect(screen.getByRole("button", { name: "Save SNOMED evidence" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Set reference concept" }));
    fireEvent.change(screen.getByLabelText("Search reference concept"), { target: { value: "pneumonia" } });
    fireEvent.click(await screen.findByRole("option", { name: /Pneumonia/ }));
    expect(get).toHaveBeenCalledWith("/v1/concepts/search/", expect.objectContaining({
      params: expect.objectContaining({ q: "pneumonia", vocabulary_id: "SNOMED" }) }));
    expect(screen.getByTestId("snomed-reference")).toHaveTextContent("Pneumonia SNOMED:233604007");
    expect(screen.getByTestId("snomed-reference")).toHaveTextContent("Chosen by curator (unsaved)");

    const hierarchy = screen.getByLabelText("Hierarchy");
    expect(Array.from((hierarchy as HTMLSelectElement).options).map(o => o.text)).toEqual(["Inferred", "None", "disorder", "finding", "procedure"]);
    fireEvent.change(hierarchy, { target: { value: "Clinical Finding" } });
    fireEvent.change(screen.getByLabelText("Laterality"), { target: { value: "__none__" } });
    expect(screen.getByTestId("snomed-laterality")).toHaveTextContent("No side");
    expect(screen.getByTestId("snomed-laterality-why")).toHaveTextContent("Cleared by curator (unsaved)");
    expect(onSettingsChange).toHaveBeenLastCalledWith({ reference_concept_id: 255848, hierarchy: "Clinical Finding", laterality: null });

    fireEvent.click(screen.getByRole("button", { name: "Save SNOMED evidence" }));
    await waitFor(() => expect(put).toHaveBeenCalledWith("/v1/code-mappings/7/snomed-attributes/", {
      snomed_attributes: { reference_concept_id: 255848, hierarchy: "Clinical Finding", laterality: null },
    }));
    expect(await screen.findByRole("status")).toHaveTextContent("SNOMED evidence saved.");
  });

  it("sends a null hierarchy for None and drops a key returned to Inferred", async () => {
    get.mockImplementation(() => Promise.resolve({ data: payload({ snomed_attributes: { laterality: "Left" } }) }));
    render(<SnomedEvidencePanel mappingId={7} canEdit />);
    await screen.findByRole("region", { name: "SNOMED evidence" });
    expect(screen.getByTestId("snomed-laterality-why")).toHaveTextContent("Set by curator");
    fireEvent.change(screen.getByLabelText("Hierarchy"), { target: { value: "__none__" } });
    fireEvent.change(screen.getByLabelText("Laterality"), { target: { value: "__inferred__" } });
    fireEvent.click(screen.getByRole("button", { name: "Save SNOMED evidence" }));
    await waitFor(() => expect(put).toHaveBeenCalledWith("/v1/code-mappings/7/snomed-attributes/", { snomed_attributes: { hierarchy: null } }));
  });

  it("renders nothing when SNOMED judging does not apply", async () => {
    get.mockImplementation(() => Promise.resolve({ data: payload({ applies: false, source: null }) }));
    const onSettingsChange = vi.fn();
    const { container } = render(<SnomedEvidencePanel mappingId={7} canEdit onSettingsChange={onSettingsChange} />);
    await waitFor(() => expect(get).toHaveBeenCalled());
    await waitFor(() => expect(container).toBeEmptyDOMElement());
    expect(onSettingsChange).not.toHaveBeenCalled();
  });

  it("hides edit controls without edit permission", async () => {
    render(<SnomedEvidencePanel mappingId={7} canEdit={false} />);
    await screen.findByRole("region", { name: "SNOMED evidence" });
    expect(screen.queryByRole("button", { name: "Set reference concept" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Hierarchy")).not.toBeInTheDocument();
  });
});
