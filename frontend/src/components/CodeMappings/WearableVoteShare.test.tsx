/**
 * Share on the Wearables tab (#2678): a curator makes a public voting link,
 * copies it, revokes it and downloads what came back.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import WearableVoteShare from "./WearableVoteShare";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockSaveBlob = vi.fn();
vi.mock("@/api/axios", () => ({
  default: { get: (...args: unknown[]) => mockGet(...args), post: (...args: unknown[]) => mockPost(...args) },
}));
vi.mock("@/utils/download", async (original) => ({
  ...(await original<typeof import("@/utils/download")>()),
  saveBlob: (...args: unknown[]) => mockSaveBlob(...args),
}));

const SHARES = "/v1/code-mappings/vote-shares/";
const share = (id: number, extra: Record<string, unknown> = {}) => ({
  id, title: `Review ${id}`, note: "", created_at: "2026-11-01T10:00:00+00:00", expires_at: null,
  revoked_at: null, active: true, voters: 0, votes: 0, url: `https://promop.example.test/vote/token-${id}`, ...extra,
});
let listed: ReturnType<typeof share>[];
const writeText = vi.fn();

async function openDialog() {
  render(<WearableVoteShare />);
  await userEvent.click(screen.getByRole("button", { name: "Share" }));
  return screen.findByRole("dialog", { name: "Share the wearable proposals for a vote" });
}
const rows = () => screen.queryAllByTestId("vote-share");

beforeEach(() => {
  mockGet.mockReset(); mockPost.mockReset(); mockSaveBlob.mockReset(); writeText.mockReset();
  listed = [];
  mockGet.mockImplementation((url: string) => (
    url === SHARES ? Promise.resolve({ data: { results: listed } }) : Promise.reject(new Error(url))
  ));
  writeText.mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  vi.spyOn(window, "confirm").mockReturnValue(true);
});
afterEach(() => { vi.restoreAllMocks(); });

describe("WearableVoteShare", () => {
  it("is one button until it is opened, and loads nothing before", async () => {
    render(<WearableVoteShare />);
    expect(screen.getByRole("button", { name: "Share" })).toBeEnabled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mockGet).not.toHaveBeenCalled();
  });

  it("explains that votes are advice and lists nothing when there are no shares", async () => {
    const dialog = await openDialog();
    expect(mockGet).toHaveBeenCalledWith(SHARES);
    expect(dialog).toHaveTextContent(/never change a mapping, and approval stays with you/);
    expect(dialog).toHaveTextContent(/only after the voter confirms it from an email/);
    expect(await screen.findByText("No link has been created yet.")).toBeInTheDocument();
    expect(screen.getByLabelText("Title (shown to voters)")).toHaveFocus();
  });

  it("creates a share and shows its public link with a Copy button", async () => {
    const made = share(5, { title: "HL7 PHR working group review" });
    mockPost.mockImplementation(() => { listed = [made]; return Promise.resolve({ data: made }); });
    await openDialog();
    await userEvent.type(screen.getByLabelText("Title (shown to voters)"), "  HL7 PHR working group review ");
    await userEvent.type(screen.getByLabelText("Note to voters (optional)"), "Vote by Friday.");
    await userEvent.type(screen.getByLabelText("Expires (optional)"), "2027-01-31");
    await userEvent.click(screen.getByRole("button", { name: "Create link" }));

    await waitFor(() => expect(mockPost).toHaveBeenCalledWith(SHARES, {
      title: "HL7 PHR working group review", note: "Vote by Friday.", expires_at: "2027-01-31",
    }));
    const link = await screen.findByLabelText("Public link for HL7 PHR working group review");
    expect(link).toHaveValue("https://promop.example.test/vote/token-5");
    expect(link).toHaveAttribute("readonly");
    await waitFor(() => expect(rows()).toHaveLength(1));
    expect(screen.getByLabelText("Title (shown to voters)")).toHaveValue("");

    const created = screen.getByRole("status");
    await userEvent.click(within(created).getByRole("button", { name: "Copy the link for HL7 PHR working group review" }));
    expect(writeText).toHaveBeenCalledWith("https://promop.example.test/vote/token-5");
    expect(within(created).getByRole("button")).toHaveTextContent("Copied");
  });

  it("sends no expiry when none was chosen, and needs a title", async () => {
    mockPost.mockResolvedValue({ data: share(6) });
    await openDialog();
    await userEvent.click(screen.getByRole("button", { name: "Create link" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/Give the share a title/);
    expect(mockPost).not.toHaveBeenCalled();

    await userEvent.type(screen.getByLabelText("Title (shown to voters)"), "Review");
    await userEvent.click(screen.getByRole("button", { name: "Create link" }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith(SHARES, { title: "Review", note: "" }));
  });

  it("shows the server's reason when a share is refused", async () => {
    mockPost.mockRejectedValue({ response: { status: 400, data: { detail: "Expiry must be in the future." } } });
    await openDialog();
    await userEvent.type(screen.getByLabelText("Title (shown to voters)"), "Review");
    await userEvent.click(screen.getByRole("button", { name: "Create link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Expiry must be in the future.");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("lists existing shares with their voters, expiry and state", async () => {
    listed = [
      share(3, { voters: 4, votes: 9, expires_at: "2027-01-31T23:59:59+00:00" }),
      share(2, { active: false, revoked_at: "2026-11-02T10:00:00+00:00", voters: 1, votes: 2 }),
      share(1, { active: false, expires_at: "2026-11-03T23:59:59+00:00" }),
    ];
    await openDialog();
    await waitFor(() => expect(rows()).toHaveLength(3));
    const [active, revoked, expired] = rows();
    expect(active).toHaveTextContent("Review 3");
    expect(active).toHaveTextContent("4 confirmed voters");
    expect(active).toHaveTextContent(/expires/);
    expect(active).toHaveTextContent("Active");
    expect(revoked).toHaveTextContent("1 confirmed voter");
    expect(revoked).toHaveTextContent("Revoked");
    expect(expired).toHaveTextContent("Expired");
    // A link that no longer opens is not offered for copying or revoking.
    expect(within(active).getByRole("button", { name: "Copy the link for Review 3" })).toBeEnabled();
    expect(within(revoked).queryByRole("button", { name: /Copy/ })).not.toBeInTheDocument();
    expect(within(revoked).queryByRole("button", { name: /Revoke/ })).not.toBeInTheDocument();
    expect(within(expired).queryByRole("button", { name: /Copy/ })).not.toBeInTheDocument();
    // Votes stay downloadable after a revoke; nothing to download without any.
    expect(within(revoked).getByRole("button", { name: "Download votes (CSV) for Review 2" })).toBeEnabled();
    expect(within(expired).getByRole("button", { name: "Download votes (CSV) for Review 1" })).toBeDisabled();
  });

  it("copies an existing share's link", async () => {
    listed = [share(3)];
    await openDialog();
    await userEvent.click(await screen.findByRole("button", { name: "Copy the link for Review 3" }));
    expect(writeText).toHaveBeenCalledWith("https://promop.example.test/vote/token-3");
    expect(screen.getByRole("button", { name: "Copy the link for Review 3" })).toHaveTextContent("Copied");
  });

  it("says so when the clipboard refuses", async () => {
    writeText.mockRejectedValue(new Error("denied"));
    listed = [share(3)];
    await openDialog();
    await userEvent.click(await screen.findByRole("button", { name: "Copy the link for Review 3" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not copy the link/);
  });

  it("revokes a share after asking, and re-reads the list", async () => {
    listed = [share(3, { voters: 2, votes: 2 })];
    mockPost.mockImplementation(() => {
      listed = [share(3, { active: false, revoked_at: "2026-11-17T10:00:00+00:00", voters: 2, votes: 2 })];
      return Promise.resolve({ data: listed[0] });
    });
    await openDialog();
    await userEvent.click(await screen.findByRole("button", { name: "Revoke Review 3" }));

    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("Confirmed votes are kept"));
    expect(mockPost).toHaveBeenCalledWith(`${SHARES}3/revoke/`);
    await waitFor(() => expect(rows()[0]).toHaveTextContent("Revoked"));
    expect(screen.queryByRole("button", { name: "Revoke Review 3" })).not.toBeInTheDocument();
  });

  it("leaves a share alone when the curator declines", async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    listed = [share(3)];
    await openDialog();
    await userEvent.click(await screen.findByRole("button", { name: "Revoke Review 3" }));
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("downloads a share's votes as the server's CSV file", async () => {
    listed = [share(3, { voters: 2, votes: 5 })];
    const file = new Blob(["x"], { type: "text/csv" });
    mockGet.mockImplementation((url: string) => (url === SHARES
      ? Promise.resolve({ data: { results: listed } })
      : Promise.resolve({ data: file, headers: { "content-disposition": 'attachment; filename="mapping-votes-share-3.csv"' } })));
    await openDialog();
    await userEvent.click(await screen.findByRole("button", { name: "Download votes (CSV) for Review 3" }));

    expect(mockGet).toHaveBeenCalledWith(`${SHARES}3/votes-export/`, { responseType: "blob" });
    await waitFor(() => expect(mockSaveBlob).toHaveBeenCalledWith(file, "mapping-votes-share-3.csv"));
  });

  it("closes on the button and on Escape", async () => {
    await openDialog();
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Share" }));
    await screen.findByRole("dialog");
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
