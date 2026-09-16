import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import PatientSurveys from "./PatientSurveys";
import type { User } from "@/hooks/useAuth";

vi.mock("@/api/axios", () => ({
  default: { get: vi.fn() },
}));

import api from "@/api/axios";

const patient = { id: 1, username: "p", is_patient: true, person_id: 42 } as unknown as User;

const survey = (over: Partial<Record<string, unknown>> = {}) => ({
  slug: "symptom-check",
  version: "1.0",
  title: "Weekly symptom check",
  url: "/s/symptom-check",
  status: "not_started",
  started_at: null,
  completed_at: null,
  ...over,
});

describe("PatientSurveys", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("lists what the runner is serving", async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ data: [survey()] });

    render(<PatientSurveys user={patient} />);

    expect(await screen.findByText("Weekly symptom check")).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith("/v1/prolog-surveys/");
  });

  it("links into the runner rather than answering here", async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ data: [survey()] });

    render(<PatientSurveys user={patient} />);

    const action = await screen.findByRole("link", { name: "Start Weekly symptom check" });
    // A real navigation out of the portal: the runner is its own application,
    // so this must stay an anchor with an href, not a click handler.
    expect(action).toHaveAttribute("href", "/s/symptom-check");
  });

  it("labels each survey by where the patient stands", async () => {
    vi.mocked(api.get).mockResolvedValueOnce({
      data: [
        survey({ slug: "a", title: "A", url: "/s/a", status: "in_progress" }),
        survey({ slug: "b", title: "B", url: "/s/b", status: "completed" }),
      ],
    });

    render(<PatientSurveys user={patient} />);

    expect(await screen.findByText("In progress")).toBeInTheDocument();
    expect(screen.getByText("Completed")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Continue A" })).toHaveAttribute("href", "/s/a");
    expect(screen.getByRole("link", { name: "View B" })).toHaveAttribute("href", "/s/b");
  });

  it("names each action link by the survey it opens", async () => {
    // Three rows would otherwise offer three links all called "Start", which
    // is what a screen reader reads out of the links list.
    vi.mocked(api.get).mockResolvedValueOnce({
      data: [
        survey({ slug: "a", title: "Pain diary", url: "/s/a" }),
        survey({ slug: "b", title: "Sleep diary", url: "/s/b" }),
      ],
    });

    render(<PatientSurveys user={patient} />);

    expect(await screen.findByRole("link", { name: "Start Pain diary" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Start Sleep diary" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("announces a load failure rather than only showing it", async () => {
    vi.mocked(api.get).mockRejectedValueOnce(new Error("nope"));

    render(<PatientSurveys user={patient} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/Failed to load surveys/);
  });

  it("says so when there is nothing to answer", async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ data: [] });

    render(<PatientSurveys user={patient} />);

    expect(await screen.findByText("No surveys available at this time.")).toBeInTheDocument();
  });

  it("offers a retry when the list cannot be loaded", async () => {
    vi.mocked(api.get).mockRejectedValueOnce(new Error("nope"));

    render(<PatientSurveys user={patient} />);

    expect(await screen.findByText(/Failed to load surveys/)).toBeInTheDocument();
    vi.mocked(api.get).mockResolvedValueOnce({ data: [survey()] });
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Weekly symptom check")).toBeInTheDocument();
  });

  it("asks for nothing when the account has no patient record", async () => {
    render(<PatientSurveys user={{ ...patient, person_id: null } as unknown as User} />);

    expect(await screen.findByText(/No health record is linked/)).toBeInTheDocument();
    await waitFor(() => expect(api.get).not.toHaveBeenCalled());
  });
});
