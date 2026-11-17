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
const mockPost = vi.fn();
vi.mock("@/api/publicAxios", () => ({
  publicApi: { get: (...args: unknown[]) => mockGet(...args), post: (...args: unknown[]) => mockPost(...args) },
}));
// The signed-in client must never be touched from a public page.
const mockPrivate = vi.fn();
vi.mock("@/api/axios", () => ({ default: { get: mockPrivate, post: mockPrivate, patch: mockPrivate, delete: mockPrivate } }));

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

function renderPage(token = "share-token") {
  return render(
    <MemoryRouter initialEntries={[`/vote/${token}`]}>
      <Routes><Route path="/vote/:token" element={<PublicVotePage />} /></Routes>
    </MemoryRouter>,
  );
}
const button = (name: string) => screen.getByRole("button", { name });
const counts = () => screen.getByTestId("vote-counts").textContent;

beforeEach(() => {
  mockGet.mockReset(); mockPost.mockReset(); mockPrivate.mockReset();
  mockGet.mockResolvedValue({ data: PAGE });
  mockPost.mockResolvedValue({ data: { detail: SENT } });
});

describe("PublicVotePage", () => {
  it("shows the title, the note and the proposals by category", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { level: 1, name: PAGE.title })).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledWith("/v1/public/mapping-votes/share-token/");
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
    // The voter is told, in view, what the address is for and who sees it.
    expect(screen.getByLabelText("Your email address")).toHaveAccessibleDescription(
      /shown to the curators beside your votes/);
    expect(screen.getByText(/shown to the curators beside your votes/)).not.toHaveClass("sr-only");
    expect(mockPrivate).not.toHaveBeenCalled();
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

  it("submits the email and only the proposals the visitor acted on", async () => {
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

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost).toHaveBeenCalledWith("/v1/public/mapping-votes/share-token/submit/", {
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
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("shows the server's reason for a refused submission and keeps the votes", async () => {
    mockPost.mockRejectedValue(failure(400, "Enter a valid email address."));
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Yes to HealthKit HKSteps" }));
    await userEvent.type(screen.getByLabelText("Your email address"), "not-an-address");
    await userEvent.click(button("Submit votes"));

    expect(await screen.findByRole("alert")).toHaveTextContent("Enter a valid email address.");
    expect(button("Yes to HealthKit HKSteps")).toHaveAttribute("aria-pressed", "true");
    expect(button("Submit votes")).toBeEnabled();
  });

  it("says so when the network is throttled or the request fails", async () => {
    mockPost.mockRejectedValueOnce(failure(429)).mockRejectedValueOnce(new Error("offline"));
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Yes to HealthKit HKSteps" }));
    await userEvent.type(screen.getByLabelText("Your email address"), "voter@example.test");

    await userEvent.click(button("Submit votes"));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Too many attempts/);
    await userEvent.click(button("Submit votes"));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Your votes could not be sent. Try again."));
  });

  it("shows one neutral state for a link that is unknown, revoked or expired", async () => {
    mockGet.mockRejectedValue(failure(404, "This link is not available."));
    renderPage("gone");
    expect(await screen.findByRole("heading", { name: "This link is not available" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Submit votes" })).not.toBeInTheDocument();
  });

  it("goes to the same state when the link is withdrawn before the votes are sent", async () => {
    mockPost.mockRejectedValue(failure(404, "This link is not available."));
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Yes to HealthKit HKSteps" }));
    await userEvent.type(screen.getByLabelText("Your email address"), "voter@example.test");
    await userEvent.click(button("Submit votes"));
    expect(await screen.findByRole("heading", { name: "This link is not available" })).toBeInTheDocument();
  });

  it("reports a failed load apart from an unavailable link", async () => {
    mockGet.mockRejectedValue(failure(500));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be loaded/);
    expect(screen.queryByText("This link is not available")).not.toBeInTheDocument();
  });

  it("says when nothing is open for voting", async () => {
    mockGet.mockResolvedValue({ data: { ...PAGE, groups: [] } });
    renderPage();
    expect(await screen.findByText(/no proposals open for voting/)).toBeInTheDocument();
  });

  it("renders what it is given as text, never as markup", async () => {
    mockGet.mockResolvedValue({ data: {
      ...PAGE, title: "<img src=x onerror=alert(1)>", note: "<script>alert(2)</script>",
      groups: [{ category: "<b>Activity</b>", proposals: [proposal(11, "<i>code</i>", { description: "<a href='x'>d</a>" })] }],
    } });
    const { container } = renderPage();
    expect(await screen.findByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(screen.getByText("<i>code</i>")).toBeInTheDocument();
    expect(container.querySelector("img, script, i, b, a")).toBeNull();
  });
});
