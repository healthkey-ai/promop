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
    const select = () => within(group()).getByLabelText("Source code system");

    it("is not offered without systems", () => {
      render(<WearableMappingsDownload />);
      expect(within(group()).queryByLabelText("Source code system")).not.toBeInTheDocument();
    });

    it("defaults to all systems and sends no filter", async () => {
      mockGet.mockResolvedValue({ data: new Blob(["x"]), headers: {} });
      render(<WearableMappingsDownload systems={SYSTEMS} />);
      expect(select()).toHaveValue("");
      await userEvent.click(csvButton());
      expect(mockGet).toHaveBeenCalledWith(URL_PATH, { params: { file_format: "csv" }, responseType: "blob" });
    });

    it("limits the file to the chosen system, for either format and either target", async () => {
      mockGet.mockResolvedValue({ data: new Blob(["x"]), headers: {} });
      render(<WearableMappingsDownload systems={SYSTEMS} />);
      await userEvent.selectOptions(select(), "Garmin");
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

    it("hands a custom fetch the chosen systems, as the public page's does not use them", async () => {
      const fetchFile = vi.fn().mockResolvedValue({ data: new Blob(["x"]), headers: {} });
      render(<WearableMappingsDownload systems={SYSTEMS} fetchFile={fetchFile} />);
      await userEvent.selectOptions(select(), "Apple");
      await userEvent.click(csvButton());
      expect(fetchFile).toHaveBeenCalledWith("csv", "omop", ["Apple"]);
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
