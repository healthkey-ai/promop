import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import SurveyForm from "./SurveyForm";
import type { Survey, SurveyResponse } from "./PatientSurveys";

const mockSurvey: Survey = {
  id: 10,
  name: "symptoms_mm",
  title: "Symptom Tracker",
  description: "Track your daily symptoms",
  status: "ACTIVE",
  disease: "mm",
  pages: [
    {
      name: "page1",
      title: "Pain Assessment",
      inputs: [
        { name: "pain_level", label: "Pain level", type: "rating", data: { maxRating: 5 } },
        { name: "pain_notes", label: "Pain notes", type: "textarea" },
      ],
    },
    {
      name: "page2",
      title: "Other Symptoms",
      inputs: [
        { name: "appetite", label: "Appetite", type: "select", data: { options: ["Good", "Fair", "Poor"] } },
        { name: "other_notes", label: "Other notes", type: "text" },
      ],
    },
  ],
  estimated_minutes: 5,
};

const mockResponse: SurveyResponse = {
  id: 100,
  person: 7,
  survey: 10,
  survey_title: "Symptom Tracker",
  survey_name: "symptoms_mm",
  values: {},
  values_dates: {},
  percent_complete: 0,
  started_at: "2025-07-25T10:00:00Z",
  completed_at: null,
  created_at: "2025-07-25T10:00:00Z",
  updated_at: "2025-07-25T10:00:00Z",
};

describe("SurveyForm", () => {
  let onSave: ReturnType<typeof vi.fn>;
  let onComplete: ReturnType<typeof vi.fn>;
  let onBack: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    onSave = vi.fn().mockResolvedValue(undefined);
    onComplete = vi.fn().mockResolvedValue(undefined);
    onBack = vi.fn();
  });

  it("renders survey title and first page title", () => {
    render(
      <SurveyForm
        survey={mockSurvey}
        response={mockResponse}
        onSave={onSave}
        onComplete={onComplete}
        onBack={onBack}
      />
    );

    expect(screen.getByText("Symptom Tracker")).toBeInTheDocument();
    expect(screen.getByText("Pain Assessment")).toBeInTheDocument();
    expect(screen.getByText("Page 1 of 2")).toBeInTheDocument();
  });

  it("renders rating inputs as numbered buttons", () => {
    render(
      <SurveyForm
        survey={mockSurvey}
        response={mockResponse}
        onSave={onSave}
        onComplete={onComplete}
        onBack={onBack}
      />
    );

    // 5 rating buttons for pain_level (maxRating: 5)
    for (let i = 1; i <= 5; i++) {
      expect(screen.getByRole("button", { name: String(i) })).toBeInTheDocument();
    }
  });

  it("renders textarea inputs", () => {
    render(
      <SurveyForm
        survey={mockSurvey}
        response={mockResponse}
        onSave={onSave}
        onComplete={onComplete}
        onBack={onBack}
      />
    );

    expect(screen.getByLabelText("Pain notes")).toBeInTheDocument();
    const textarea = screen.getByLabelText("Pain notes");
    expect(textarea.tagName.toLowerCase()).toBe("textarea");
  });

  it("renders select inputs on page 2", () => {
    render(
      <SurveyForm
        survey={mockSurvey}
        response={mockResponse}
        onSave={onSave}
        onComplete={onComplete}
        onBack={onBack}
      />
    );

    // Navigate to page 2
    const nextBtn = screen.getByRole("button", { name: /next/i });
    fireEvent.click(nextBtn);

    expect(screen.getByLabelText("Appetite")).toBeInTheDocument();
    const select = screen.getByLabelText("Appetite");
    expect(select.tagName.toLowerCase()).toBe("select");

    // Check options
    const options = select.querySelectorAll("option");
    expect(options.length).toBe(4); // "Select..." + 3 options
  });

  it("navigates between pages", async () => {
    render(
      <SurveyForm
        survey={mockSurvey}
        response={mockResponse}
        onSave={onSave}
        onComplete={onComplete}
        onBack={onBack}
      />
    );

    // Start on page 1
    expect(screen.getByText("Pain Assessment")).toBeInTheDocument();
    expect(screen.getByText("Page 1 of 2")).toBeInTheDocument();

    // Navigate to page 2
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(screen.getByText("Other Symptoms")).toBeInTheDocument();
    expect(screen.getByText("Page 2 of 2")).toBeInTheDocument();

    // Wait for async save to complete so Previous button is enabled
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /previous/i })).not.toBeDisabled();
    });

    // Navigate back to page 1
    fireEvent.click(screen.getByRole("button", { name: /previous/i }));
    expect(screen.getByText("Pain Assessment")).toBeInTheDocument();
    expect(screen.getByText("Page 1 of 2")).toBeInTheDocument();
  });

  it("calls onSave on page change", async () => {
    render(
      <SurveyForm
        survey={mockSurvey}
        response={mockResponse}
        onSave={onSave}
        onComplete={onComplete}
        onBack={onBack}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /next/i }));

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledWith(
        expect.any(Object),
        expect.any(Number)
      );
    });
  });

  it("shows Complete Survey button on last page", () => {
    render(
      <SurveyForm
        survey={mockSurvey}
        response={mockResponse}
        onSave={onSave}
        onComplete={onComplete}
        onBack={onBack}
      />
    );

    // Page 1 should not show Complete button
    expect(screen.queryByRole("button", { name: /complete survey/i })).not.toBeInTheDocument();

    // Navigate to last page
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(screen.getByRole("button", { name: /complete survey/i })).toBeInTheDocument();
  });

  it("calls onComplete when Complete Survey is clicked", async () => {
    render(
      <SurveyForm
        survey={mockSurvey}
        response={mockResponse}
        onSave={onSave}
        onComplete={onComplete}
        onBack={onBack}
      />
    );

    // Navigate to last page
    fireEvent.click(screen.getByRole("button", { name: /next/i }));

    // Click complete
    fireEvent.click(screen.getByRole("button", { name: /complete survey/i }));

    await waitFor(() => {
      expect(onComplete).toHaveBeenCalled();
    });
  });

  it("disables inputs in readOnly mode", () => {
    render(
      <SurveyForm
        survey={mockSurvey}
        response={{ ...mockResponse, completed_at: "2025-07-25T11:00:00Z" }}
        onSave={onSave}
        onComplete={onComplete}
        onBack={onBack}
        readOnly
      />
    );

    const textarea = screen.getByLabelText("Pain notes");
    expect(textarea).toBeDisabled();
    expect(screen.getByText(/completed.*read only/i)).toBeInTheDocument();
    // Should not show Complete Survey button
    expect(screen.queryByRole("button", { name: /complete survey/i })).not.toBeInTheDocument();
  });

  it("calls onBack when Back to surveys is clicked", () => {
    render(
      <SurveyForm
        survey={mockSurvey}
        response={mockResponse}
        onSave={onSave}
        onComplete={onComplete}
        onBack={onBack}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /back to surveys/i }));
    expect(onBack).toHaveBeenCalled();
  });

  it("updates rating value when clicked", () => {
    render(
      <SurveyForm
        survey={mockSurvey}
        response={mockResponse}
        onSave={onSave}
        onComplete={onComplete}
        onBack={onBack}
      />
    );

    const ratingBtn = screen.getByRole("button", { name: "3" });
    fireEvent.click(ratingBtn);

    // The button should now have the selected styling (primary bg)
    expect(ratingBtn.className).toContain("bg-primary");
  });

  it("renders with existing response values", () => {
    render(
      <SurveyForm
        survey={mockSurvey}
        response={{
          ...mockResponse,
          values: { pain_notes: "Mild back pain" },
        }}
        onSave={onSave}
        onComplete={onComplete}
        onBack={onBack}
      />
    );

    const textarea = screen.getByLabelText("Pain notes") as HTMLTextAreaElement;
    expect(textarea.value).toBe("Mild back pain");
  });
});
