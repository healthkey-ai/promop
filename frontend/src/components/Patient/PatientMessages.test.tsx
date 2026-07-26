import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import PatientMessages from "./PatientMessages";
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

const mockThreads = [
  {
    id: 10,
    patient_user: 1,
    parent: null,
    sender: 2,
    sender_name: "Dr. Smith",
    subject: "Lab results available",
    message: "Your recent lab results are ready for review.",
    sender_is_patient: false,
    is_read: false,
    read_at: null,
    reply_count: 2,
    created_at: "2026-07-20T14:30:00Z",
  },
  {
    id: 11,
    patient_user: 1,
    parent: null,
    sender: 1,
    sender_name: "Pat",
    subject: "Question about medication",
    message: "I have a question about my dosage.",
    sender_is_patient: true,
    is_read: true,
    read_at: "2026-07-19T10:00:00Z",
    reply_count: 0,
    created_at: "2026-07-19T09:00:00Z",
  },
];

const mockReplies = [
  {
    id: 20,
    patient_user: 1,
    parent: 10,
    sender: 1,
    sender_name: "Pat",
    subject: "Re: Lab results available",
    message: "Thank you, I will review them.",
    sender_is_patient: true,
    is_read: true,
    read_at: null,
    reply_count: 0,
    created_at: "2026-07-20T15:00:00Z",
  },
  {
    id: 21,
    patient_user: 1,
    parent: 10,
    sender: 2,
    sender_name: "Dr. Smith",
    subject: "Re: Lab results available",
    message: "Let me know if you have questions.",
    sender_is_patient: false,
    is_read: false,
    read_at: null,
    reply_count: 0,
    created_at: "2026-07-20T16:00:00Z",
  },
];

describe("PatientMessages", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders thread list after loading", async () => {
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      data: mockThreads,
    });

    render(<PatientMessages user={makeUser()} />);

    await waitFor(() => {
      expect(screen.getByText("Lab results available")).toBeInTheDocument();
    });
    expect(screen.getByText("Question about medication")).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith("/v1/messages/?parent=null");
  });

  it("shows unread indicator for unread provider messages", async () => {
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      data: mockThreads,
    });

    render(<PatientMessages user={makeUser()} />);

    await waitFor(() => {
      expect(screen.getByText("Lab results available")).toBeInTheDocument();
    });

    // The unread thread subject should be bold
    const unreadSubject = screen.getByText("Lab results available");
    expect(unreadSubject.className).toContain("font-bold");

    // The read thread subject should not be bold
    const readSubject = screen.getByText("Question about medication");
    expect(readSubject.className).not.toContain("font-bold");
  });

  it("shows reply count badge", async () => {
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      data: mockThreads,
    });

    render(<PatientMessages user={makeUser()} />);

    await waitFor(() => {
      expect(screen.getByText("2 replies")).toBeInTheDocument();
    });
  });

  it("clicking a thread opens conversation view", async () => {
    (api.get as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ data: mockThreads })       // initial thread list
      .mockResolvedValueOnce({ data: mockReplies });       // replies fetch

    (api.patch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      data: { ...mockThreads[0], is_read: true, read_at: "2026-07-25T10:00:00Z" },
    });

    render(<PatientMessages user={makeUser()} />);

    await waitFor(() => {
      expect(screen.getByText("Lab results available")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Lab results available"));

    await waitFor(() => {
      expect(
        screen.getByText("Your recent lab results are ready for review.")
      ).toBeInTheDocument();
    });

    // Should show replies
    expect(
      screen.getByText("Thank you, I will review them.")
    ).toBeInTheDocument();
    expect(
      screen.getByText("Let me know if you have questions.")
    ).toBeInTheDocument();

    // Should have called mark-read for unread provider message
    expect(api.patch).toHaveBeenCalledWith("/v1/messages/10/mark-read/");

    // Should show back button
    expect(screen.getByText("Back to messages")).toBeInTheDocument();
  });

  it("shows compose form when New Message is clicked", async () => {
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      data: mockThreads,
    });

    render(<PatientMessages user={makeUser()} />);

    await waitFor(() => {
      expect(screen.getByText("New Message")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("New Message"));

    expect(screen.getByLabelText("Subject")).toBeInTheDocument();
    expect(screen.getByLabelText("Message")).toBeInTheDocument();
    expect(screen.getByText("Send")).toBeInTheDocument();
    expect(screen.getByText("Cancel")).toBeInTheDocument();
  });

  it("sends message on compose submit", async () => {
    (api.get as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ data: mockThreads })     // initial load
      .mockResolvedValueOnce({ data: mockThreads });    // refresh after send

    (api.post as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      data: { id: 12, subject: "New topic", message: "Hello", sender_is_patient: true },
    });

    render(<PatientMessages user={makeUser()} />);

    await waitFor(() => {
      expect(screen.getByText("New Message")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("New Message"));

    const subjectInput = screen.getByLabelText("Subject");
    const messageInput = screen.getByLabelText("Message");

    fireEvent.change(subjectInput, { target: { value: "New topic" } });
    fireEvent.change(messageInput, { target: { value: "Hello" } });

    fireEvent.click(screen.getByText("Send"));

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith("/v1/messages/", {
        subject: "New topic",
        message: "Hello",
      });
    });
  });

  it("shows error message when fetch fails", async () => {
    (api.get as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("Network error")
    );

    render(<PatientMessages user={makeUser()} />);

    await waitFor(() => {
      expect(
        screen.getByText(/failed to load messages/i)
      ).toBeInTheDocument();
    });
  });

  it("shows no-record message when user has no person_id", () => {
    render(<PatientMessages user={makeUser({ person_id: null })} />);

    expect(
      screen.getByText(/no health record is linked/i)
    ).toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalled();
  });

  it("shows no-record message when user is null", () => {
    render(<PatientMessages user={null} />);

    expect(
      screen.getByText(/no health record is linked/i)
    ).toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalled();
  });
});
