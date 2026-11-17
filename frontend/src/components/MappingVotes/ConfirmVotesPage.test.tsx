/**
 * The page the emailed link opens (#2678): it shows the submitted votes and
 * records them only when the voter presses Confirm.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ConfirmVotesPage from "./ConfirmVotesPage";
import type { PendingVotes } from "./voteTypes";

const mockGet = vi.fn();
const mockPost = vi.fn();
vi.mock("@/api/publicAxios", () => ({
  publicApi: { get: (...args: unknown[]) => mockGet(...args), post: (...args: unknown[]) => mockPost(...args) },
}));
const mockPrivate = vi.fn();
vi.mock("@/api/axios", () => ({ default: { get: mockPrivate, post: mockPrivate, patch: mockPrivate, delete: mockPrivate } }));

const URL_PATH = "/v1/public/mapping-votes/confirm/confirm-token/";
const PENDING: PendingVotes = {
  title: "HL7 PHR working group review", email: "voter@example.test", expires_at: "2026-11-19T10:00:00+00:00",
  votes: [
    { proposal: {
      id: 11, metric_key: "steps", measure_class: "", source: "HealthKit", source_code: "HKSteps",
      description: "Step count", destination_vocabulary: "LOINC", destination_code: "55423-8",
      destination_name: "Number of steps", unit: "",
    }, choice: "yes", comment: "Agree\nwith a second line" },
    { proposal: {
      id: 13, metric_key: "sleep", measure_class: "", source: "Health Connect", source_code: "SleepSession",
      description: "", destination_vocabulary: "SNOMED", destination_code: "248263006",
      destination_name: "Duration of sleep", unit: "min",
    }, choice: "", comment: "Which stage?" },
    { proposal: null, choice: "no", comment: "" },
  ],
};
const failure = (status: number) => ({ response: { status, data: { detail: "This confirmation link is no longer valid." } } });

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/vote/confirm/confirm-token"]}>
      <Routes><Route path="/vote/confirm/:confirmToken" element={<ConfirmVotesPage />} /></Routes>
    </MemoryRouter>,
  );
}
const INVALID = "This confirmation link is no longer valid";

beforeEach(() => {
  mockGet.mockReset(); mockPost.mockReset(); mockPrivate.mockReset();
  mockGet.mockResolvedValue({ data: PENDING });
  mockPost.mockResolvedValue({ data: { recorded: 2, skipped: 1 } });
});

describe("ConfirmVotesPage", () => {
  it("shows the submitted votes and confirms nothing by opening", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { name: "Confirm your votes" })).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledWith(URL_PATH);
    expect(screen.getByText("voter@example.test")).toBeInTheDocument();
    expect(screen.getByText(/Nothing is recorded until you confirm/)).toBeInTheDocument();
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("HKSteps");
    expect(items[0]).toHaveTextContent("LOINC:55423-8");
    expect(items[0]).toHaveTextContent("Your vote: Yes");
    expect(items[0]).toHaveTextContent("Agree with a second line");
    expect(items[1]).toHaveTextContent("Your vote: Comment only");
    expect(items[1]).toHaveTextContent("Which stage?");
    expect(items[2]).toHaveTextContent(/This vote will be skipped/);
    expect(items[2]).toHaveTextContent("Your vote: No");
    expect(mockPost).not.toHaveBeenCalled();
    expect(mockPrivate).not.toHaveBeenCalled();
  });

  it("records the votes when the voter confirms and reports what was skipped", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Confirm my votes" }));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost).toHaveBeenCalledWith(URL_PATH);
    expect(await screen.findByRole("heading", { name: "Thank you" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("2 votes recorded for HL7 PHR working group review.");
    expect(screen.getByTestId("skipped-votes")).toHaveTextContent(/^1 proposal was skipped/);
    expect(screen.queryByRole("button", { name: "Confirm my votes" })).not.toBeInTheDocument();
  });

  it("says nothing about skipping when every vote was recorded", async () => {
    mockPost.mockResolvedValue({ data: { recorded: 1, skipped: 0 } });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Confirm my votes" }));
    expect(await screen.findByRole("status")).toHaveTextContent("1 vote recorded");
    expect(screen.queryByTestId("skipped-votes")).not.toBeInTheDocument();
  });

  it("says when none could be recorded", async () => {
    mockPost.mockResolvedValue({ data: { recorded: 0, skipped: 3 } });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Confirm my votes" }));
    expect(await screen.findByRole("status")).toHaveTextContent("None of your votes could be recorded.");
    expect(screen.getByTestId("skipped-votes")).toHaveTextContent(/^3 proposals were skipped/);
  });

  it("shows one neutral state for a link that is used, expired or unknown", async () => {
    mockGet.mockRejectedValue(failure(404));
    renderPage();
    expect(await screen.findByRole("heading", { name: INVALID })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm my votes" })).not.toBeInTheDocument();
  });

  it("goes to the same state when the link was used in another tab meanwhile", async () => {
    mockPost.mockRejectedValue(failure(404));
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Confirm my votes" }));
    expect(await screen.findByRole("heading", { name: INVALID })).toBeInTheDocument();
  });

  it("lets the voter try again after a failure that is not the link's", async () => {
    mockPost.mockRejectedValueOnce(failure(500)).mockResolvedValueOnce({ data: { recorded: 2, skipped: 0 } });
    renderPage();
    const confirm = await screen.findByRole("button", { name: "Confirm my votes" });
    await userEvent.click(confirm);
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be confirmed/);
    await userEvent.click(screen.getByRole("button", { name: "Confirm my votes" }));
    expect(await screen.findByRole("heading", { name: "Thank you" })).toBeInTheDocument();
  });

  it("reports a failed load apart from an invalid link", async () => {
    mockGet.mockRejectedValue(failure(503));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be loaded/);
    expect(screen.queryByText(INVALID)).not.toBeInTheDocument();
  });

  it("renders a comment as text", async () => {
    mockGet.mockResolvedValue({ data: {
      ...PENDING, votes: [{ ...PENDING.votes[0], comment: "<img src=x onerror=alert(1)>" }],
    } });
    const { container } = renderPage();
    expect(await screen.findByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });
});
