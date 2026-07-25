import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import PatientSurveys from "./PatientSurveys";
import type { User } from "@/hooks/useAuth";

vi.mock("@/api/axios", () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
  },
}));

import api from "@/api/axios";

const makeUser = (overrides: Partial<User> = {}): User => ({
  id: 1,
  sub: "sub",
  email: "p@test.com",
  name: "Pat",
  is_patient: true,
  person_id: 7,
  ...overrides,
});

const mockSurveys = [
  {
    id: 10,
    name: "symptoms_mm",
    title: "Symptom Tracker",
    description: "Track your daily symptoms",
    status: "ACTIVE",
    disease: "mm",
    pages: [
      {
        name: "page1",
        title: "Symptoms",
        inputs: [
          { name: "fatigue", label: "Fatigue level", type: "rating", data: { maxRating: 10 } },
          { name: "pain_notes", label: "Pain notes", type: "textarea" },
        ],
      },
    ],
    estimated_minutes: 5,
  },
  {
    id: 11,
    name: "quality_of_life",
    title: "Quality of Life",
    description: "Assess your overall well-being",
    status: "ACTIVE",
    disease: "mm",
    pages: [
      {
        name: "page1",
        title: "Well-being",
        inputs: [
          { name: "mood", label: "Mood", type: "select", data: { options: ["Good", "Fair", "Poor"] } },
        ],
      },
    ],
    estimated_minutes: 3,
  },
];

const mockResponseInProgress = {
  id: 100,
  person: 7,
  survey: 10,
  survey_title: "Symptom Tracker",
  survey_name: "symptoms_mm",
  values: { fatigue: 5 },
  values_dates: {},
  percent_complete: 50,
  started_at: "2025-07-20T10:00:00Z",
  completed_at: null,
  created_at: "2025-07-20T10:00:00Z",
  updated_at: "2025-07-20T10:30:00Z",
};

const mockResponseCompleted = {
  id: 101,
  person: 7,
  survey: 11,
  survey_title: "Quality of Life",
  survey_name: "quality_of_life",
  values: { mood: "Good" },
  values_dates: {},
  percent_complete: 100,
  started_at: "2025-07-19T10:00:00Z",
  completed_at: "2025-07-19T10:15:00Z",
  created_at: "2025-07-19T10:00:00Z",
  updated_at: "2025-07-19T10:15:00Z",
};

describe("PatientSurveys", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders survey list after loading", async () => {
    (api.get as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ data: mockSurveys })
      .mockResolvedValueOnce({ data: [] });

    render(<PatientSurveys user={makeUser()} />);

    await waitFor(() => {
      expect(screen.getByText("Symptom Tracker")).toBeInTheDocument();
    });
    expect(screen.getByText("Quality of Life")).toBeInTheDocument();
    expect(screen.getByText("~5 min")).toBeInTheDocument();
    expect(screen.getByText("~3 min")).toBeInTheDocument();
  });

  it("shows 'Not started' for surveys without a response", async () => {
    (api.get as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ data: mockSurveys })
      .mockResolvedValueOnce({ data: [] });

    render(<PatientSurveys user={makeUser()} />);

    await waitFor(() => {
      expect(screen.getByText("Symptom Tracker")).toBeInTheDocument();
    });
    const badges = screen.getAllByText("Not started");
    expect(badges.length).toBe(2);
  });

  it("shows 'In progress' with percentage for partial responses", async () => {
    (api.get as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ data: mockSurveys })
      .mockResolvedValueOnce({ data: [mockResponseInProgress] });

    render(<PatientSurveys user={makeUser()} />);

    await waitFor(() => {
      expect(screen.getByText("In progress (50%)")).toBeInTheDocument();
    });
  });

  it("shows 'Completed' for completed responses", async () => {
    (api.get as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ data: mockSurveys })
      .mockResolvedValueOnce({ data: [mockResponseCompleted] });

    render(<PatientSurveys user={makeUser()} />);

    await waitFor(() => {
      expect(screen.getByText("Completed")).toBeInTheDocument();
    });
  });

  it("Start button creates a response via POST", async () => {
    (api.get as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ data: [mockSurveys[0]] })
      .mockResolvedValueOnce({ data: [] });

    const newResponse = {
      id: 200,
      person: 7,
      survey: 10,
      survey_title: "Symptom Tracker",
      survey_name: "symptoms_mm",
      values: {},
      values_dates: {},
      percent_complete: 0,
      started_at: "2025-07-25T12:00:00Z",
      completed_at: null,
      created_at: "2025-07-25T12:00:00Z",
      updated_at: "2025-07-25T12:00:00Z",
    };
    (api.post as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      data: newResponse,
    });

    render(<PatientSurveys user={makeUser()} />);

    await waitFor(() => {
      expect(screen.getByText("Symptom Tracker")).toBeInTheDocument();
    });

    const startBtn = screen.getByRole("button", { name: "Start" });
    fireEvent.click(startBtn);

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        "/v1/survey-responses/",
        expect.objectContaining({
          person: 7,
          survey: 10,
        })
      );
    });
  });

  it("shows error message when fetch fails", async () => {
    (api.get as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("Network error")
    );

    render(<PatientSurveys user={makeUser()} />);

    await waitFor(() => {
      expect(
        screen.getByText(/failed to load surveys/i)
      ).toBeInTheDocument();
    });
  });

  it("shows no-record message when user has no person_id", () => {
    render(<PatientSurveys user={makeUser({ person_id: null })} />);

    expect(
      screen.getByText(/no health record is linked/i)
    ).toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalled();
  });

  it("shows no-record message when user is null", () => {
    render(<PatientSurveys user={null} />);

    expect(
      screen.getByText(/no health record is linked/i)
    ).toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalled();
  });

  it("shows Continue button for in-progress response", async () => {
    (api.get as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ data: [mockSurveys[0]] })
      .mockResolvedValueOnce({ data: [mockResponseInProgress] });

    render(<PatientSurveys user={makeUser()} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Continue" })).toBeInTheDocument();
    });
  });

  it("shows View button for completed response", async () => {
    (api.get as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ data: [mockSurveys[1]] })
      .mockResolvedValueOnce({ data: [mockResponseCompleted] });

    render(<PatientSurveys user={makeUser()} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "View" })).toBeInTheDocument();
    });
  });
});
