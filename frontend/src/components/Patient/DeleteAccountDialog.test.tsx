import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import DeleteAccountDialog from "./DeleteAccountDialog";

// Mock the axios instance
vi.mock("@/api/axios", () => ({
  default: {
    delete: vi.fn(),
  },
}));

import api from "@/api/axios";

describe("DeleteAccountDialog", () => {
  const onClose = vi.fn();
  const onDeleted = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders warning text and confirm input", () => {
    render(<DeleteAccountDialog onClose={onClose} onDeleted={onDeleted} />);
    expect(screen.getByText(/permanently delete/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/DELETE/)).toBeInTheDocument();
  });

  it("confirm button is disabled until DELETE is typed", () => {
    render(<DeleteAccountDialog onClose={onClose} onDeleted={onDeleted} />);
    const btn = screen.getByRole("button", { name: /delete my account/i });
    expect(btn).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/DELETE/), { target: { value: "DELETE" } });
    expect(btn).not.toBeDisabled();
  });

  it("calls API and onDeleted on successful delete", async () => {
    (api.delete as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ data: {} });

    render(<DeleteAccountDialog onClose={onClose} onDeleted={onDeleted} />);
    fireEvent.change(screen.getByLabelText(/DELETE/), { target: { value: "DELETE" } });
    fireEvent.click(screen.getByRole("button", { name: /delete my account/i }));

    await waitFor(() => {
      expect(api.delete).toHaveBeenCalledWith("/api/v1/patient-records/me/", {
        data: { confirm: "DELETE" },
      });
      expect(onDeleted).toHaveBeenCalled();
    });
  });

  it("shows error message on API failure", async () => {
    (api.delete as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
      response: { data: { detail: "Something went wrong" } },
    });

    render(<DeleteAccountDialog onClose={onClose} onDeleted={onDeleted} />);
    fireEvent.change(screen.getByLabelText(/DELETE/), { target: { value: "DELETE" } });
    fireEvent.click(screen.getByRole("button", { name: /delete my account/i }));

    await waitFor(() => {
      expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    });
    expect(onDeleted).not.toHaveBeenCalled();
  });

  it("calls onClose when Cancel is clicked", () => {
    render(<DeleteAccountDialog onClose={onClose} onDeleted={onDeleted} />);
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onClose).toHaveBeenCalled();
  });
});
