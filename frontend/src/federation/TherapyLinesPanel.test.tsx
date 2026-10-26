import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { AxiosInstance } from 'axios';

import { PatientFormStore } from './patientFormStore';
import TherapyLinesPanel from './TherapyLinesPanel';

const completeTherapyHistory = vi.fn(async () => ({ therapy_history_complete: true, patient_info: {} }));
vi.mock('@/api/therapyLines', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/therapyLines')>()),
  setTherapyHistoryCompleteness: (...args: unknown[]) => completeTherapyHistory(...(args as [])),
}));

async function setup(patient: Record<string, unknown>, writeEnabled = true) {
  const get = vi.fn(async (url: string) => ({ data: url.includes('writable-fields') ? {
    therapies: { writable: false, target: 'episode', label: 'Therapies' },
  } : {
    patient_info: { person_id: 12, disease: 'Multiple Myeloma', write_enabled: writeEnabled, ...patient },
    write_enabled: writeEnabled,
  } }));
  const store = new PatientFormStore({ get } as unknown as AxiosInstance, '/api');
  await store.load();
  return { store };
}

describe('therapy lines editor', () => {
  it('lists the recorded lines with their date range', async () => {
    const { store } = await setup({
      therapy_lines_count: '2',
      lines_of_therapy: [
        { line: 1, episode_id: 11, regimen: 'VRd', outcome: 'Partial response', start_date: '2024-01-01' },
        { line: 2, episode_id: 12, regimen: 'Dara-Pd', start_date: '2024-06-01', end_date: '2024-10-01' },
      ],
    });

    render(<TherapyLinesPanel store={store} readOnly={false} />);

    expect(screen.getByText(/Line 1:/)).toBeInTheDocument();
    expect(screen.getByText(/VRd/)).toBeInTheDocument();
    expect(screen.getByText(/2024-06-01 to 2024-10-01/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Add therapy line/i })).toBeInTheDocument();
  });

  it('says so when no line is recorded and hides the editor when read-only', async () => {
    const { store } = await setup({ therapy_lines_count: '0', lines_of_therapy: [] });

    render(<TherapyLinesPanel store={store} readOnly />);

    expect(screen.getByText('No therapy lines recorded.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Add therapy line/i })).not.toBeInTheDocument();
  });

  it('tells the host after a mutation, because the store load alone does not', async () => {
    const { store } = await setup({ therapy_lines_count: '1', lines_of_therapy: [], therapy_history_complete: false });
    const onChanged = vi.fn();

    render(<TherapyLinesPanel store={store} onChanged={onChanged} />);
    fireEvent.click(screen.getByLabelText('Treatment history is complete'));

    await waitFor(() => expect(completeTherapyHistory).toHaveBeenCalledWith(12, true));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it('shows the line count as a lower bound until the history is confirmed', async () => {
    const { store } = await setup({ therapy_lines_count: '1', lines_of_therapy: [], therapy_history_complete: false });

    render(<TherapyLinesPanel store={store} />);

    expect(screen.getByLabelText('Treatment history is complete')).not.toBeChecked();
    expect(screen.getByText(/counts as a minimum/i)).toBeInTheDocument();
  });
});
