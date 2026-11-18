/**
 * The public voting page (#2678): a visitor with a shared link and no
 * account marks Yes, No or a comment and submits with an email address.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PublicVotePage from "./PublicVotePage";
import type { VotePage } from "./voteTypes";

const mockGet = vi.fn();
const mockPage = vi.fn();
const mockSubmit = vi.fn();
const PAGE_URL = "/v1/public/mapping-votes/";
const SUBMIT_URL = "/v1/public/mapping-votes/submit/";
const EXPORT_URL = "/v1/public/mapping-votes/export/";
const mockExport = vi.fn();
const mockSaveBlob = vi.fn();
vi.mock("@/api/publicAxios", () => ({
  publicApi: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (url: string, ...rest: unknown[]) => (
      url === SUBMIT_URL ? mockSubmit(url, ...rest) : url === EXPORT_URL ? mockExport(url, ...rest) : mockPage(url, ...rest)),
  },
}));
vi.mock("@/utils/download", async (original) => ({
  ...(await original<typeof import("@/utils/download")>()),
  saveBlob: (...args: unknown[]) => mockSaveBlob(...args),
}));
// The signed-in client must never be touched from a public page.
const mockPrivate = vi.fn();
// Called lazily: the download control imports the signed-in client's module, so
// this factory runs before `mockPrivate` is initialised.
vi.mock("@/api/axios", () => {
  const viaPrivate = (...args: unknown[]) => mockPrivate(...args);
  return { default: { get: viaPrivate, post: viaPrivate, patch: viaPrivate, delete: viaPrivate } };
});

const proposal = (id: number, code: string, extra: Partial<VotePage["groups"][0]["proposals"][0]> = {}) => ({
  id, metric_key: "steps", measure_class: "Aggregate over interval", source: "HealthKit", source_code: code,
  description: "Step count", destination_vocabulary: "LOINC", destination_code: "55423-8",
  destination_name: "Number of steps", unit: "{steps}", ...extra,
});
const PAGE: VotePage = {
  title: "HL7 PHR working group review", note: "Please vote by Friday.", expires_at: null, comment_max_length: 1000,
  groups: [
    { category: "Activity", proposals: [proposal(11, "HKSteps"), proposal(12, "Steps", { source: "Health Connect" })] },
    { category: "Sleep", proposals: [proposal(13, "SleepSession", { metric_key: "sleep", unit: "" })] },
  ],
};
const SENT = "Check your email to confirm your votes. They are not recorded until you confirm.";
const failure = (status: number, detail?: string) => ({ response: { status, data: detail ? { detail } : {} } });

function renderPage(location = "/vote#share-token") {
  return render(
    <MemoryRouter initialEntries={[location]}>
      <Routes><Route path="/vote" element={<PublicVotePage />} /></Routes>
    </MemoryRouter>,
  );
}
const button = (name: string) => screen.getByRole("button", { name });
const counts = () => screen.getByTestId("vote-counts").textContent;

beforeEach(() => {
  [mockGet, mockPage, mockSubmit, mockPrivate, mockExport, mockSaveBlob].forEach((mock) => mock.mockReset());
  mockPage.mockResolvedValue({ data: PAGE });
  mockSubmit.mockResolvedValue({ data: { detail: SENT } });
});

describe("PublicVotePage", () => {
  it("shows the title, the note and the proposals by category", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { level: 1, name: PAGE.title })).toBeInTheDocument();
    expect(screen.getByText("Please vote by Friday.")).toBeInTheDocument();
    expect(screen.getByText(/not recorded until you confirm/)).toBeInTheDocument();
    const activity = screen.getByRole("region", { name: "Activity" });
    expect(within(activity).getAllByRole("listitem")).toHaveLength(2);
    expect(within(activity).getByText("HKSteps")).toBeInTheDocument();
    expect(within(activity).getAllByText("LOINC:55423-8")).toHaveLength(2);
    expect(within(activity).getAllByText("Number of steps")).toHaveLength(2);
    expect(within(activity).getAllByText("Unit: {steps}")).toHaveLength(2);
    const sleep = screen.getByRole("region", { name: "Sleep" });
    expect(within(sleep).getByText("Unit: none set")).toBeInTheDocument();
    expect(counts()).toBe("0 votes, 0 comments");
    expect(mockPrivate).not.toHaveBeenCalled();
  });

  it("says which source systems a limited link is limited to, and nothing for an unlimited one", async () => {
    mockPage.mockResolvedValue({ data: { ...PAGE, source_systems: ["HealthKit", "Garmin"] } });
    const { unmount } = renderPage();
    expect(await screen.findByTestId("vote-source-systems")).toHaveTextContent("Limited to: HealthKit, Garmin");
    unmount();
    mockPage.mockResolvedValue({ data: { ...PAGE, source_systems: [] } });
    renderPage();
    await screen.findByRole("heading", { level: 1, name: PAGE.title });
    expect(screen.queryByTestId("vote-source-systems")).not.toBeInTheDocument();
  });

  it("takes the token from the URL fragment and sends it only in a request body", async () => {
    renderPage("/vote#share-token");
    await screen.findByRole("heading", { level: 1, name: PAGE.title });
    // No GET at all, and no URL that carries the token: a token in a path
    // or a query string is in every server log.
    expect(mockGet).not.toHaveBeenCalled();
    expect(mockPage.mock.calls).toEqual([[PAGE_URL, { token: "share-token" }]]);
    await userEvent.click(button("Yes to HealthKit HKSteps"));
    await userEvent.type(screen.getByLabelText("Your email address"), "voter@example.test");
    await userEvent.click(button("Submit votes"));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1));
    for (const [url] of [...mockPage.mock.calls, ...mockSubmit.mock.calls]) {
      expect(url).not.toContain("share-token");
    }
  });

  it("tells the voter, in view, what is stored and that votes are advisory", async () => {
    renderPage();
    const email = await screen.findByLabelText("Your email address");
    expect(email).toHaveAccessibleDescription(
      /address and comments are stored and shown to this organisation's mapping administrators/);
    const notice = screen.getByText(/stored and shown to this organisation's mapping administrators/);
    expect(notice).toHaveTextContent("Votes are advisory.");
    expect(notice).not.toHaveClass("sr-only");
  });

  it("toggles Yes and No as a two-state choice that a second click clears", async () => {
    renderPage();
    const yes = await screen.findByRole("button", { name: "Yes to HealthKit HKSteps" });
    const no = button("No to HealthKit HKSteps");
    expect(yes).toHaveAttribute("aria-pressed", "false");

    await userEvent.click(yes);
    expect(yes).toHaveAttribute("aria-pressed", "true");
    expect(no).toHaveAttribute("aria-pressed", "false");
    expect(counts()).toBe("1 vote, 0 comments");

    await userEvent.click(no);
    expect(yes).toHaveAttribute("aria-pressed", "false");
    expect(no).toHaveAttribute("aria-pressed", "true");
    expect(counts()).toBe("1 vote, 0 comments");

    await userEvent.click(no);
    expect(no).toHaveAttribute("aria-pressed", "false");
    expect(counts()).toBe("0 votes, 0 comments");
  });

  it("opens a labelled comment box, counts it and removes it again", async () => {
    renderPage();
    const toggle = await screen.findByRole("button", { name: "Comment on HealthKit HKSteps" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Comment on HealthKit HKSteps", { selector: "textarea" })).not.toBeInTheDocument();

    await userEvent.click(toggle);
    const box = screen.getByLabelText("Comment on HealthKit HKSteps", { selector: "textarea" });
    expect(box).toHaveAttribute("maxlength", "1000");
    await userEvent.type(box, "Check the unit");
    expect(counts()).toBe("0 votes, 1 comment");
    expect(screen.getByText("14 / 1000")).toBeInTheDocument();

    await userEvent.click(toggle);
    expect(screen.queryByLabelText("Comment on HealthKit HKSteps", { selector: "textarea" })).not.toBeInTheDocument();
    expect(counts()).toBe("0 votes, 0 comments");
  });

  it("submits the token, the email and only the proposals the visitor acted on", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Yes to HealthKit HKSteps" }));
    await userEvent.click(button("Comment on HealthKit HKSteps"));
    await userEvent.type(screen.getByLabelText("Comment on HealthKit HKSteps", { selector: "textarea" }), "  Agree  ");
    await userEvent.click(button("Comment on HealthKit SleepSession"));
    await userEvent.type(screen.getByLabelText("Comment on HealthKit SleepSession", { selector: "textarea" }), "Which stage?");
    // Chosen, then cleared: says nothing, so it is not sent.
    await userEvent.click(button("No to Health Connect Steps"));
    await userEvent.click(button("No to Health Connect Steps"));
    expect(counts()).toBe("1 vote, 2 comments");
    await userEvent.type(screen.getByLabelText("Your email address"), " voter@example.test ");

    await userEvent.click(button("Submit votes"));

    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1));
    expect(mockSubmit).toHaveBeenCalledWith(SUBMIT_URL, {
      token: "share-token",
      email: "voter@example.test",
      votes: [
        { mapping: 11, choice: "yes", comment: "Agree" },
        { mapping: 13, choice: "", comment: "Which stage?" },
      ],
    });
    expect(await screen.findByRole("status")).toHaveTextContent(SENT);
    expect(screen.queryByRole("button", { name: "Submit votes" })).not.toBeInTheDocument();
    expect(mockPrivate).not.toHaveBeenCalled();
  });

  it("asks for a vote and an address before sending anything", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Submit votes" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/at least one proposal/);

    await userEvent.click(button("Yes to HealthKit HKSteps"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await userEvent.click(button("Submit votes"));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter your email address.");
    expect(mockSubmit).not.toHaveBeenCalled();
  });

  it("shows the server's reason for a refused submission and keeps the votes", async () => {
    mockSubmit.mockRejectedValue(failure(400, "Enter a valid email address."));
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Yes to HealthKit HKSteps" }));
    await userEvent.type(screen.getByLabelText("Your email address"), "not-an-address");
    await userEvent.click(button("Submit votes"));

    expect(await screen.findByRole("alert")).toHaveTextContent("Enter a valid email address.");
    expect(button("Yes to HealthKit HKSteps")).toHaveAttribute("aria-pressed", "true");
    expect(button("Submit votes")).toBeEnabled();
  });

  it("says so when voting is unavailable, throttled or the request fails, and never says check your email", async () => {
    mockSubmit
      .mockRejectedValueOnce(failure(503, "Voting is temporarily unavailable. Please try again later."))
      .mockRejectedValueOnce(failure(429))
      .mockRejectedValueOnce(new Error("offline"));
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Yes to HealthKit HKSteps" }));
    await userEvent.type(screen.getByLabelText("Your email address"), "voter@example.test");

    await userEvent.click(button("Submit votes"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Voting is temporarily unavailable. Please try again later.");
    expect(screen.queryByText(SENT)).not.toBeInTheDocument();
    await userEvent.click(button("Submit votes"));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/Too many attempts/));
    await userEvent.click(button("Submit votes"));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Your votes could not be sent. Try again."));
    expect(button("Yes to HealthKit HKSteps")).toHaveAttribute("aria-pressed", "true");
  });

  it("shows one neutral state for a link that is unknown, revoked or expired", async () => {
    mockPage.mockRejectedValue(failure(404, "This link is not available."));
    renderPage("/vote#gone");
    expect(await screen.findByRole("heading", { name: "This link is not available" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Submit votes" })).not.toBeInTheDocument();
  });

  it("shows the same state, without asking the server, for a link with no token", async () => {
    renderPage("/vote");
    expect(await screen.findByRole("heading", { name: "This link is not available" })).toBeInTheDocument();
    expect(mockPage).not.toHaveBeenCalled();
  });

  it("goes to the same state when the link is withdrawn before the votes are sent", async () => {
    mockSubmit.mockRejectedValue(failure(404, "This link is not available."));
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Yes to HealthKit HKSteps" }));
    await userEvent.type(screen.getByLabelText("Your email address"), "voter@example.test");
    await userEvent.click(button("Submit votes"));
    expect(await screen.findByRole("heading", { name: "This link is not available" })).toBeInTheDocument();
  });

  it("reports a failed load apart from an unavailable link", async () => {
    mockPage.mockRejectedValue(failure(500));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be loaded/);
    expect(screen.queryByText("This link is not available")).not.toBeInTheDocument();
  });

  it("says when nothing is open for voting", async () => {
    mockPage.mockResolvedValue({ data: { ...PAGE, groups: [] } });
    renderPage();
    expect(await screen.findByText(/no proposals open for voting/)).toBeInTheDocument();
  });

  it("renders what it is given as text, never as markup", async () => {
    mockPage.mockResolvedValue({ data: {
      ...PAGE, title: "<img src=x onerror=alert(1)>", note: "<script>alert(2)</script>",
      groups: [{ category: "<b>Activity</b>", proposals: [proposal(11, "<i>code</i>", { description: "<a href='x'>d</a>" })] }],
    } });
    const { container } = renderPage();
    expect(await screen.findByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(screen.getByText("<i>code</i>")).toBeInTheDocument();
    expect(container.querySelector("img, script, i, b, a")).toBeNull();
  });

  it("downloads the mappings with the link's token in the body, never the signed-in client", async () => {
    const file = new Blob(["x"]);
    mockExport.mockResolvedValue({
      data: file, headers: { "content-disposition": 'attachment; filename="wearable-cdisc-bc-mappings-2026-11-18.csv"' },
    });
    renderPage();
    const group = await screen.findByRole("group", { name: "Download all wearable mappings" });
    await userEvent.selectOptions(within(group).getByRole("combobox", { name: "Map codes to" }), "cdisc-bc");
    await userEvent.click(within(group).getByRole("button", { name: "Download all wearable mappings as CSV" }));
    expect(mockExport).toHaveBeenCalledWith(
      EXPORT_URL, { token: "share-token", file_format: "csv", destination: "cdisc-bc" }, { responseType: "blob" });
    await waitFor(() => expect(mockSaveBlob).toHaveBeenCalledWith(file, "wearable-cdisc-bc-mappings-2026-11-18.csv"));
    expect(mockPrivate).not.toHaveBeenCalled();
    expect(mockGet).not.toHaveBeenCalled();
  });
});
