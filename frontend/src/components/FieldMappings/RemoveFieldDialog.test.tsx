import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { RemoveFieldDialog } from "./RemoveFieldDialog";
import { RemovedFieldsPanel } from "./RemovedFieldsPanel";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockDelete = vi.fn();

vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));

const usage = (changes = {}) => ({
  field_name: "gleason_grade_group", retired: false, records_with_value: 3, has_facts: true,
  answers: 2, can_delete: false, ...changes,
});

describe("RemoveFieldDialog", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();
    mockDelete.mockReset();
  });

  it("states how many records hold a value and offers only the reversible removal", async () => {
    mockGet.mockResolvedValue({ data: usage() });
    render(<RemoveFieldDialog fieldName="gleason_grade_group" onClose={vi.fn()} onDone={vi.fn()} />);

    expect(await screen.findByTestId("usage-summary")).toHaveTextContent("3 records hold a value");
    expect(screen.getByText(/can restore the field later/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete permanently" })).not.toBeInTheDocument();
    expect(screen.getByText(/cannot be deleted permanently/)).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledWith("/v1/custom-patient-fields/gleason_grade_group/");
  });

  it("retires on Remove field and reports the server's answer", async () => {
    mockGet.mockResolvedValue({ data: usage() });
    mockDelete.mockResolvedValue({ data: { state: "retired" } });
    const onDone = vi.fn();
    render(<RemoveFieldDialog fieldName="gleason_grade_group" onClose={vi.fn()} onDone={onDone} />);

    fireEvent.click(await screen.findByRole("button", { name: "Remove field" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledWith("retired", "gleason_grade_group"));
    expect(mockDelete).toHaveBeenCalledWith("/v1/custom-patient-fields/gleason_grade_group/", undefined);
  });

  it("offers a permanent delete only when nothing depends on the field", async () => {
    mockGet.mockResolvedValue({ data: usage({ records_with_value: 0, has_facts: false, can_delete: true }) });
    mockDelete.mockResolvedValue({ data: { state: "deleted" } });
    const onDone = vi.fn();
    render(<RemoveFieldDialog fieldName="gleason_grade_group" onClose={vi.fn()} onDone={onDone} />);

    expect(await screen.findByTestId("usage-summary")).toHaveTextContent("No record holds a value");
    fireEvent.click(screen.getByRole("button", { name: "Delete permanently" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledWith("deleted", "gleason_grade_group"));
    expect(mockDelete).toHaveBeenCalledWith(
      "/v1/custom-patient-fields/gleason_grade_group/", { params: { permanent: "true" } });
  });

  it("shows the server's refusal and the counts it now holds", async () => {
    mockGet
      .mockResolvedValueOnce({ data: usage({ records_with_value: 0, has_facts: false, can_delete: true }) })
      .mockResolvedValueOnce({ data: usage({ records_with_value: 1, can_delete: false }) });
    mockDelete.mockRejectedValue(Object.assign(new Error("conflict"), {
      isAxiosError: true, response: { status: 409, data: { detail: "Records still hold values for this field." } },
    }));
    render(<RemoveFieldDialog fieldName="gleason_grade_group" onClose={vi.fn()} onDone={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "Delete permanently" }));

    expect(await screen.findByText("Records still hold values for this field.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("usage-summary")).toHaveTextContent("1 record hold"));
    expect(screen.queryByRole("button", { name: "Delete permanently" })).not.toBeInTheDocument();
  });

  it("for a removed field offers no second removal, only the permanent delete when allowed", async () => {
    mockGet.mockResolvedValue({ data: usage({ retired: true, records_with_value: 0, has_facts: false, can_delete: true }) });
    render(<RemoveFieldDialog fieldName="gleason_grade_group" onClose={vi.fn()} onDone={vi.fn()} />);

    expect(await screen.findByRole("button", { name: "Delete permanently" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove field" })).not.toBeInTheDocument();
  });
});

describe("RemovedFieldsPanel", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();
  });

  it("lists the removed fields and restores one", async () => {
    mockGet.mockResolvedValue({ data: [{
      field_name: "gleason_grade_group", display_name: "Gleason grade group", tab: "disease",
      field_type: "choice", retired_at: "2026-11-27T10:00:00Z",
    }] });
    mockPost.mockResolvedValue({ data: {} });
    const onChanged = vi.fn();
    render(<RemovedFieldsPanel revision={0} onChanged={onChanged} />);

    expect(await screen.findByText("Gleason grade group")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledWith("/v1/custom-patient-fields/", { params: { state: "retired" } });
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith("restored", "gleason_grade_group"));
    expect(mockPost).toHaveBeenCalledWith("/v1/custom-patient-fields/gleason_grade_group/restore/");
  });

  it("says when nothing was removed", async () => {
    mockGet.mockResolvedValue({ data: [] });
    render(<RemovedFieldsPanel revision={0} onChanged={vi.fn()} />);
    expect(await screen.findByText("No removed fields.")).toBeInTheDocument();
  });
});

describe("RemoveFieldDialog keyboard and races", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockDelete.mockReset();
  });

  it("focuses Cancel on open and closes on Escape", async () => {
    mockGet.mockResolvedValue({ data: usage() });
    const onClose = vi.fn();
    render(<RemoveFieldDialog fieldName="gleason_grade_group" onClose={onClose} onDone={vi.fn()} />);
    await screen.findByTestId("usage-summary");

    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("treats a 404 on removal as done, so the page refreshes", async () => {
    mockGet.mockResolvedValue({ data: usage() });
    mockDelete.mockRejectedValue(Object.assign(new Error("gone"), {
      isAxiosError: true, response: { status: 404, data: { detail: "Not found." } },
    }));
    const onDone = vi.fn();
    render(<RemoveFieldDialog fieldName="gleason_grade_group" onClose={vi.fn()} onDone={onDone} />);

    fireEvent.click(await screen.findByRole("button", { name: "Remove field" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledWith("deleted", "gleason_grade_group"));
  });
});
