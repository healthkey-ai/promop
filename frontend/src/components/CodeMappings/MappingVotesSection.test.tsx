/** The Votes section of the Edit Mapping dialog and the list's tally (#2678). */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MappingVotesSection from "./MappingVotesSection";
import VoteTally from "./VoteTally";
import { tallyText } from "./voteCounts";

const mockGet = vi.fn();
vi.mock("@/api/axios", () => ({ default: { get: (...args: unknown[]) => mockGet(...args) } }));

const vote = (id: number, extra: Record<string, unknown> = {}) => ({
  id, choice: "yes", comment: "", confirmed_at: "2026-11-10T09:00:00+00:00", share_id: 1,
  share_title: "HL7 review", stale: false, ...extra,
});
const payload = (votes: unknown[], tally = { yes: 2, no: 1, comments: 1 }, emails = true) => ({
  data: { tally, emails_included: emails, votes },
});

beforeEach(() => { mockGet.mockReset(); });

describe("MappingVotesSection", () => {
  it("is closed, with the tally on its heading, until it is opened", async () => {
    mockGet.mockResolvedValue(payload([
      vote(3, { voter_email: "a@example.test", comment: "Right concept" }),
      vote(2, { voter_email: "b@example.test", choice: "no" }),
      vote(1, { voter_email: "c@example.test" }),
    ]));
    render(<MappingVotesSection mappingId={42} />);

    const toggle = await screen.findByRole("button", { name: /Votes/ });
    expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/42/votes/");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByTestId("section-marker")).toHaveTextContent("2 yes · 1 no · 1 comment");
    expect(screen.queryAllByTestId("mapping-vote")).toHaveLength(0);

    await userEvent.click(toggle);
    const votes = screen.getAllByTestId("mapping-vote");
    expect(votes).toHaveLength(3);
    expect(votes[0]).toHaveTextContent("Yes");
    expect(votes[0]).toHaveTextContent("a@example.test");
    expect(votes[0]).toHaveTextContent("HL7 review");
    expect(votes[0]).toHaveTextContent("Right concept");
    expect(votes[1]).toHaveTextContent("No");
    expect(screen.getByText(/a vote never changes this mapping/)).toBeInTheDocument();
  });

  it("shows choices and comments without addresses to a reader the server gave none", async () => {
    mockGet.mockResolvedValue(payload([vote(1, { comment: "Looks right" })], { yes: 1, no: 0, comments: 1 }, false));
    const { container } = render(<MappingVotesSection mappingId={42} />);
    await userEvent.click(await screen.findByRole("button", { name: /Votes/ }));
    expect(screen.getByTestId("mapping-vote")).toHaveTextContent("Looks right");
    expect(container.textContent).not.toContain("@");
  });

  it("marks a vote on an earlier destination and keeps it out of the heading's tally", async () => {
    mockGet.mockResolvedValue(payload(
      [vote(2, { choice: "", comment: "Unit?" }), vote(1, { stale: true })], { yes: 0, no: 0, comments: 1 }));
    render(<MappingVotesSection mappingId={42} />);
    expect(await screen.findByTestId("section-marker")).toHaveTextContent("1 comment · 1 on an earlier destination");
    await userEvent.click(screen.getByRole("button", { name: /Votes/ }));
    const [current, stale] = screen.getAllByTestId("mapping-vote");
    expect(current).toHaveTextContent("Comment");
    expect(current).not.toHaveTextContent("on an earlier destination");
    expect(stale).toHaveTextContent("on an earlier destination");
  });

  it("is not there at all for a mapping nobody voted on", async () => {
    mockGet.mockResolvedValue(payload([], { yes: 0, no: 0, comments: 0 }));
    const { container } = render(<MappingVotesSection mappingId={42} />);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("says so when the votes cannot be loaded", async () => {
    mockGet.mockRejectedValue(new Error("offline"));
    render(<MappingVotesSection mappingId={42} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("The votes could not be loaded.");
  });

  it("renders a comment as text", async () => {
    mockGet.mockResolvedValue(payload([vote(1, { comment: "<img src=x onerror=alert(1)>" })]));
    const { container } = render(<MappingVotesSection mappingId={42} />);
    await userEvent.click(await screen.findByRole("button", { name: /Votes/ }));
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });
});

describe("VoteTally", () => {
  it("names only what there is", () => {
    expect(tallyText({ yes: 3, no: 1, comments: 2 })).toBe("3 yes · 1 no · 2 comments");
    expect(tallyText({ yes: 0, no: 2, comments: 1 })).toBe("2 no · 1 comment");
    expect(tallyText({ yes: 0, no: 0, comments: 0 })).toBe("");
    expect(tallyText(undefined)).toBe("");
  });

  it("renders nothing for a row without votes", () => {
    const { container, rerender } = render(<VoteTally votes={{ yes: 0, no: 0, comments: 0 }} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<VoteTally />);
    expect(container).toBeEmptyDOMElement();
    rerender(<VoteTally votes={{ yes: 3, no: 1, comments: 2 }} />);
    expect(screen.getByTestId("vote-tally")).toHaveTextContent("Votes: 3 yes · 1 no · 2 comments");
  });
});
