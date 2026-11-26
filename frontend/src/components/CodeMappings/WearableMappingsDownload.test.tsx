/**
 * The Wearables tab's own download: the whole tab as a CSV or a Word
 * document, built by the server.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import WearableMappingsDownload from "./WearableMappingsDownload";

const mockGet = vi.fn();
const mockSaveBlob = vi.fn();
vi.mock("@/api/axios", () => ({ default: { get: (...args: unknown[]) => mockGet(...args) } }));
vi.mock("@/utils/download", async (original) => ({
  ...(await original<typeof import("@/utils/download")>()),
  saveBlob: (...args: unknown[]) => mockSaveBlob(...args),
}));

const URL_PATH = "/v1/code-mappings/wearables-export/";
const group = () => screen.getByRole("group", { name: "Download all wearable mappings" });
const CSV = "Download all wearable mappings as CSV";
const WORD = "Download all wearable mappings as a Word document";
const csvButton = () => within(group()).getByRole("button", { name: CSV });
const wordButton = () => within(group()).getByRole("button", { name: WORD });

beforeEach(() => { mockGet.mockReset(); mockSaveBlob.mockReset(); });

describe("WearableMappingsDownload", () => {
  it("reads source systems, then target, then the files, under its label", () => {
    render(<WearableMappingsDownload systems={[{ id: "Apple", label: "HealthKit" }, { id: "Garmin", label: "Garmin" }]} />);
    expect(group()).toHaveTextContent(/^Download all wearable mappings\s*All source systems\s*OMOP conceptCDISC Biomedical Concept\s*CSV\s*Word document$/);
  });

  it("offers a CSV and a Word document under one label", () => {
    render(<WearableMappingsDownload />);
    expect(group()).toHaveTextContent("Download all wearable mappings");
    expect(within(group()).getAllByRole("button").map((button) => button.textContent)).toEqual(["CSV", "Word document"]);
    // Each is named in full, so neither is taken for a section menu's "CSV".
    expect(csvButton()).toBeEnabled();
    expect(wordButton()).toBeEnabled();
    expect(mockGet).not.toHaveBeenCalled();
  });

  it.each([
    [CSV, "csv", "text/csv"],
    [WORD, "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
  ])("%s: fetches a blob and saves it under the server's filename", async (label, format, type) => {
    const file = new Blob(["x"], { type });
    mockGet.mockResolvedValue({
      data: file,
      headers: { "content-disposition": `attachment; filename="wearable-mappings-2026-03-09.${format}"` },
    });
    render(<WearableMappingsDownload />);
    await userEvent.click(within(group()).getByRole("button", { name: label }));
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalledWith(URL_PATH, { params: { file_format: format }, responseType: "blob" });
    await waitFor(() => expect(mockSaveBlob).toHaveBeenCalledWith(file, `wearable-mappings-2026-03-09.${format}`));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  describe("source code systems", () => {
    const SYSTEMS = [{ id: "Apple", label: "HealthKit" }, { id: "Garmin", label: "Garmin" }];
    /** The closed control: it names what is chosen. */
    const systemsButton = () => within(group()).getByRole("button", { name: /^Source code systems:/ });
    const list = () => screen.getByRole("group", { name: "Source code systems" });
    /** Ticks or unticks a system, opening the list first when it is shut. */
    const tick = async (label: string) => {
      if (systemsButton().getAttribute("aria-expanded") !== "true") await userEvent.click(systemsButton());
      await userEvent.click(within(list()).getByLabelText(label));
    };

    it("is not offered without systems", () => {
      render(<WearableMappingsDownload />);
      expect(within(group()).queryByRole("button", { name: /^Source code systems:/ })).not.toBeInTheDocument();
    });

    it("defaults to all systems and sends no filter", async () => {
      mockGet.mockResolvedValue({ data: new Blob(["x"]), headers: {} });
      render(<WearableMappingsDownload systems={SYSTEMS} />);
      expect(systemsButton()).toHaveTextContent("All source systems");
      await userEvent.click(csvButton());
      expect(mockGet).toHaveBeenCalledWith(URL_PATH, { params: { file_format: "csv" }, responseType: "blob" });
    });

    it("limits the file to several systems at once, in the order the systems are listed (#2795)", async () => {
      mockGet.mockResolvedValue({ data: new Blob(["x"]), headers: {} });
      const systems = [...SYSTEMS, { id: "HealthConnect", label: "Health Connect" }, { id: "HL7-PHR", label: "HL7 PHR" }];
      render(<WearableMappingsDownload systems={systems} />);
      await tick("HL7 PHR");
      await tick("HealthKit");
      expect(systemsButton()).toHaveTextContent("HealthKit, HL7 PHR");
      await tick("Health Connect");
      expect(systemsButton()).toHaveTextContent("3 source systems");
      await userEvent.click(csvButton());
      expect(mockGet).toHaveBeenLastCalledWith(URL_PATH, {
        params: { file_format: "csv", sources: "Apple,HealthConnect,HL7-PHR" }, responseType: "blob",
      });
      // Unticking one narrows it; "All source systems" clears the rest.
      await tick("HealthKit");
      expect(systemsButton()).toHaveTextContent("Health Connect, HL7 PHR");
      await tick("All source systems");
      expect(systemsButton()).toHaveTextContent("All source systems");
      await userEvent.click(csvButton());
      expect(mockGet).toHaveBeenLastCalledWith(URL_PATH, { params: { file_format: "csv" }, responseType: "blob" });
    });

    it("closes the list on Escape and on a click elsewhere, keeping what was ticked", async () => {
      render(<WearableMappingsDownload systems={SYSTEMS} />);
      await tick("Garmin");
      await userEvent.keyboard("{Escape}");
      expect(screen.queryByRole("group", { name: "Source code systems" })).not.toBeInTheDocument();
      expect(systemsButton()).toHaveFocus();
      expect(systemsButton()).toHaveTextContent("Garmin");
      await userEvent.click(systemsButton());
      expect(within(list()).getByLabelText("Garmin")).toBeChecked();
      expect(within(list()).getByLabelText("All source systems")).not.toBeChecked();
      await userEvent.click(document.body);
      expect(screen.queryByRole("group", { name: "Source code systems" })).not.toBeInTheDocument();
    });

    it("treats every box ticked as all systems (#2799)", async () => {
      mockGet.mockResolvedValue({ data: new Blob(["x"]), headers: {} });
      render(<WearableMappingsDownload systems={SYSTEMS} />);
      await tick("HealthKit");
      expect(systemsButton()).toHaveTextContent("HealthKit");
      await tick("Garmin");
      expect(systemsButton()).toHaveTextContent("All source systems");
      expect(within(list()).getByLabelText("All source systems")).toBeChecked();
      expect(within(list()).getByLabelText("HealthKit")).not.toBeChecked();
      await userEvent.click(csvButton());
      expect(mockGet).toHaveBeenLastCalledWith(URL_PATH, { params: { file_format: "csv" }, responseType: "blob" });
    });

    it("is a disclosure: the button names the list it opens, and closes it when focus moves on (#2799)", async () => {
      mockGet.mockResolvedValue({ data: new Blob(["x"]), headers: {} });
      render(<WearableMappingsDownload systems={SYSTEMS} />);
      expect(systemsButton()).toHaveAttribute("aria-expanded", "false");
      expect(systemsButton()).not.toHaveAttribute("aria-haspopup");
      systemsButton().focus();
      await userEvent.keyboard("{Enter}");
      expect(systemsButton()).toHaveAttribute("aria-expanded", "true");
      expect(systemsButton()).toHaveAttribute("aria-controls", list().id);
      // All, HealthKit, Garmin, and then out of the list.
      await userEvent.tab();
      await userEvent.tab();
      await userEvent.keyboard(" ");
      await userEvent.tab();
      expect(list()).toBeInTheDocument();
      await userEvent.tab();
      expect(screen.queryByRole("group", { name: "Source code systems" })).not.toBeInTheDocument();
      expect(systemsButton()).toHaveTextContent("HealthKit");
      // A download does not bring the list back.
      await userEvent.click(csvButton());
      await waitFor(() => expect(mockSaveBlob).toHaveBeenCalledTimes(1));
      expect(screen.queryByRole("group", { name: "Source code systems" })).not.toBeInTheDocument();
    });

    it("closes on Escape when the focus is not in the list", async () => {
      render(<WearableMappingsDownload systems={SYSTEMS} />);
      await userEvent.click(systemsButton());
      (document.activeElement as HTMLElement | null)?.blur();
      await userEvent.keyboard("{Escape}");
      expect(screen.queryByRole("group", { name: "Source code systems" })).not.toBeInTheDocument();
    });

    it("limits the file to a single ticked system, for either format and either target", async () => {
      mockGet.mockResolvedValue({ data: new Blob(["x"]), headers: {} });
      render(<WearableMappingsDownload systems={SYSTEMS} />);
      await tick("Garmin");
      await userEvent.click(wordButton());
      expect(mockGet).toHaveBeenLastCalledWith(URL_PATH, {
        params: { file_format: "docx", sources: "Garmin" }, responseType: "blob",
      });
      await userEvent.selectOptions(within(group()).getByLabelText("Map codes to"), "cdisc-bc");
      await userEvent.click(csvButton());
      expect(mockGet).toHaveBeenLastCalledWith(URL_PATH, {
        params: { file_format: "csv", destination: "cdisc-bc", sources: "Garmin" }, responseType: "blob",
      });
    });

    it("does not offer the CDISC BC system under the CDISC BC target, and drops a choice of it", async () => {
      mockGet.mockResolvedValue({ data: new Blob(["x"]), headers: {} });
      const systems = [...SYSTEMS, { id: "CDISC-BC", label: "CDISC BC" }];
      render(<WearableMappingsDownload systems={systems} />);
      await tick("CDISC BC");
      expect(systemsButton()).toHaveTextContent("CDISC BC");
      await userEvent.selectOptions(within(group()).getByLabelText("Map codes to"), "cdisc-bc");
      expect(systemsButton()).toHaveTextContent("All source systems");
      await userEvent.click(systemsButton());
      expect(within(list()).queryByLabelText("CDISC BC")).not.toBeInTheDocument();
      expect(within(list()).getByLabelText("HealthKit")).toBeInTheDocument();
      await userEvent.click(csvButton());
      expect(mockGet).toHaveBeenLastCalledWith(URL_PATH, {
        params: { file_format: "csv", destination: "cdisc-bc" }, responseType: "blob",
      });
    });

    it("forgets a CDISC BC tick once the target hides it, whatever else is ticked (#2799)", async () => {
      const systems = [...SYSTEMS, { id: "HL7-PHR", label: "HL7 PHR" }, { id: "CDISC-BC", label: "CDISC BC" }];
      render(<WearableMappingsDownload systems={systems} />);
      await tick("HealthKit");
      await tick("CDISC BC");
      expect(systemsButton()).toHaveTextContent("HealthKit, CDISC BC");
      const target = within(group()).getByLabelText("Map codes to");
      await userEvent.selectOptions(target, "cdisc-bc");
      expect(systemsButton()).toHaveTextContent("HealthKit");
      await userEvent.selectOptions(target, "omop");
      expect(systemsButton()).toHaveTextContent("HealthKit");
      await userEvent.click(systemsButton());
      expect(within(list()).getByLabelText("CDISC BC")).not.toBeChecked();
    });

    it("hands a custom fetch the chosen systems, as the public page's does not use them", async () => {
      const fetchFile = vi.fn().mockResolvedValue({ data: new Blob(["x"]), headers: {} });
      render(<WearableMappingsDownload systems={[...SYSTEMS, { id: "HL7-PHR", label: "HL7 PHR" }]} fetchFile={fetchFile} />);
      await tick("HealthKit");
      await tick("Garmin");
      await userEvent.click(csvButton());
      expect(fetchFile).toHaveBeenCalledWith("csv", "omop", ["Apple", "Garmin"]);
    });
  });

  describe("the From / To layout of the Concept to Code row (#2795)", () => {
    const SYSTEMS = [{ id: "Apple", label: "HealthKit" }, { id: "Garmin", label: "Garmin" }];

    it("reads From the systems, To the target, then CSV and Word, without the sentence", () => {
      render(<WearableMappingsDownload systems={SYSTEMS} layout="from-to" />);
      // The label is still the group's name, and each button's, for a screen reader.
      expect(group()).toHaveTextContent(/^From\s*All source systems\s*To\s*OMOP conceptCDISC Biomedical Concept\s*CSV\s*Word$/);
      expect(within(group()).getAllByRole("button").map((button) => button.textContent?.trim()))
        .toEqual(["All source systems", "CSV", "Word"]);
      for (const button of [csvButton(), wordButton()]) {
        expect(button.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
      }
    });

    it("holds From's place, disabled on all systems, until the systems are known (#2799)", () => {
      render(<WearableMappingsDownload layout="from-to" />);
      expect(group()).toHaveTextContent(/^From\s*All source systems\s*To/);
      expect(within(group()).getByRole("button", { name: "Source code systems: All source systems" })).toBeDisabled();
    });

    it("lists the systems in plain weight under the From label", async () => {
      render(<WearableMappingsDownload systems={SYSTEMS} layout="from-to" />);
      await userEvent.click(within(group()).getByRole("button", { name: /^Source code systems:/ }));
      expect(screen.getByLabelText("HealthKit").closest("label")).toHaveClass("font-normal");
      expect(screen.getByLabelText("All source systems").closest("label")).toHaveClass("font-medium");
      expect(screen.getByText("From").closest("span")).not.toContainElement(screen.getByLabelText("HealthKit"));
    });

    it("downloads what From and To say", async () => {
      mockGet.mockResolvedValue({ data: new Blob(["x"]), headers: {} });
      render(<WearableMappingsDownload systems={[...SYSTEMS, { id: "HL7-PHR", label: "HL7 PHR" }]} layout="from-to" />);
      await userEvent.click(within(group()).getByRole("button", { name: /^Source code systems:/ }));
      await userEvent.click(screen.getByLabelText("HealthKit"));
      await userEvent.click(screen.getByLabelText("Garmin"));
      await userEvent.selectOptions(within(group()).getByLabelText("Map codes to"), "cdisc-bc");
      await userEvent.click(wordButton());
      expect(mockGet).toHaveBeenLastCalledWith(URL_PATH, {
        params: { file_format: "docx", destination: "cdisc-bc", sources: "Apple,Garmin" }, responseType: "blob",
      });
    });
  });

  it("names the file itself when the response carries no filename", async () => {
    const file = new Blob(["x"]);
    mockGet.mockResolvedValue({ data: file, headers: {} });
    render(<WearableMappingsDownload />);
    await userEvent.click(wordButton());
    await waitFor(() => expect(mockSaveBlob).toHaveBeenCalledWith(file, "wearable-mappings.docx"));
  });

  it("can be worked from the keyboard", async () => {
    mockGet.mockResolvedValue({ data: new Blob(["x"]), headers: {} });
    render(<WearableMappingsDownload />);
    await userEvent.tab();
    expect(within(group()).getByRole("combobox", { name: "Map codes to" })).toHaveFocus();
    await userEvent.tab();
    expect(csvButton()).toHaveFocus();
    await userEvent.tab();
    expect(wordButton()).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(mockSaveBlob).toHaveBeenCalledTimes(1));
    expect(mockGet).toHaveBeenCalledWith(URL_PATH, { params: { file_format: "docx" }, responseType: "blob" });
  });

  it("says it is preparing the file and takes no second request until it arrives", async () => {
    let finish: (value: unknown) => void = () => {};
    mockGet.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    render(<WearableMappingsDownload />);
    await userEvent.click(wordButton());

    const preparing = within(group()).getByRole("button", { name: "Preparing…" });
    expect(preparing).toBeDisabled();
    expect(preparing).toHaveAttribute("aria-busy", "true");
    // The other choice keeps its name, and waits too.
    expect(csvButton()).toBeDisabled();
    expect(csvButton()).toHaveAttribute("aria-busy", "false");
    await userEvent.click(csvButton());
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockSaveBlob).not.toHaveBeenCalled();

    finish({ data: new Blob(["x"]), headers: {} });
    await waitFor(() => expect(wordButton()).toBeEnabled());
    expect(csvButton()).toBeEnabled();
    expect(mockSaveBlob).toHaveBeenCalledTimes(1);
  });

  it("shows the server's reason when the download is refused", async () => {
    // With responseType "blob" the error body is a Blob as well.
    mockGet.mockRejectedValue({
      response: { status: 403, data: new Blob([JSON.stringify({ detail: "Organization admin access required." })]) },
    });
    render(<WearableMappingsDownload />);
    await userEvent.click(csvButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("Organization admin access required.");
    expect(mockSaveBlob).not.toHaveBeenCalled();
    expect(csvButton()).toBeEnabled();
    expect(wordButton()).toBeEnabled();
  });

  it("says the download failed when there is no reason, and clears that on a retry that works", async () => {
    mockGet.mockRejectedValueOnce(new Error("Network Error"));
    render(<WearableMappingsDownload />);
    await userEvent.click(wordButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not download the wearable mappings. Try again.");

    mockGet.mockResolvedValue({ data: new Blob(["x"]), headers: {} });
    await userEvent.click(wordButton());
    await waitFor(() => expect(mockSaveBlob).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("asks for the CDISC Biomedical Concept as the target when chosen", async () => {
    const file = new Blob(["x"]);
    mockGet.mockResolvedValue({
      data: file,
      headers: { "content-disposition": 'attachment; filename="wearable-cdisc-bc-mappings-2026-03-09.docx"' },
    });
    render(<WearableMappingsDownload />);
    const target = within(group()).getByRole("combobox", { name: "Map codes to" });
    expect(target).toHaveValue("omop");
    await userEvent.selectOptions(target, "cdisc-bc");
    await userEvent.click(wordButton());
    expect(mockGet).toHaveBeenCalledWith(URL_PATH, {
      params: { file_format: "docx", destination: "cdisc-bc" }, responseType: "blob",
    });
    await waitFor(() => expect(mockSaveBlob).toHaveBeenCalledWith(file, "wearable-cdisc-bc-mappings-2026-03-09.docx"));
  });
});
