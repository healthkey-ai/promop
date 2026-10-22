import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { AxiosInstance } from 'axios';
import PatientFieldEditor from './PatientFieldEditor';
import { PatientFormStore } from './patientFormStore';

async function setup(extraFields = {}) {
  const fields = {
    height: { writable: true, target: 'patient_record', control: 'number', label: 'Height', unit: 'cm' },
    weight: { writable: true, target: 'patient_record', control: 'number', label: 'Weight', unit: 'kg' },
    bmi: { writable: false, kind: 'computed', label: 'BMI', inputs: ['height', 'weight'], bindings: ['bmiMin'] },
    ...extraFields,
  };
  const get = vi.fn(async (url: string) => ({ data: url.includes('writable-fields') ? fields : {
    patient_info: { person_id: 12, height: 170, weight: 60, bmi: 20.76 }, write_enabled: true,
  } }));
  const patch = vi.fn(async (_url: string, values: Record<string, unknown>) => ({ data: {
    patient_info: { person_id: 12, height: 170, weight: 60, ...values, bmi: 27.68 },
    write: { applied: Object.keys(values) },
  } }));
  const store = new PatientFormStore({ get, patch } as unknown as AxiosInstance, '/api');
  await store.load();
  return { store, patch };
}

describe('computed patient field input dialogs', () => {
  it('opens editable inputs and changes the server-computed result in both displays', async () => {
    const { store, patch } = await setup();
    render(<><PatientFieldEditor store={store} name="bmiMin" /><PatientFieldEditor store={store} name="bmi" /></>);
    fireEvent.click(screen.getAllByRole('button', { name: 'Edit BMI inputs' })[0]);
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByDisplayValue('170')).toBeEnabled();
    fireEvent.change(within(dialog).getByDisplayValue('60'), { target: { value: '80' } });
    await act(async () => { await store.flush(); });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    expect(screen.getAllByText('27.68')).toHaveLength(2);
    expect(patch.mock.calls[0][1]).toEqual({ weight: 80 });
    expect(patch.mock.calls[0][1]).not.toHaveProperty('bmi');
  });
  it('recursively opens a computed child instead of making its verdict writable', async () => {
    const { store } = await setup({ composite: { label: 'Composite', writable: false, inputs: ['bmi'] } });
    render(<PatientFieldEditor store={store} name="composite" />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit Composite inputs' }));
    fireEvent.click(screen.getByRole('button', { name: 'Edit BMI inputs' }));
    expect(screen.getByDisplayValue('170')).toBeEnabled();
    expect(screen.queryByRole('textbox', { name: 'BMI' })).not.toBeInTheDocument();
  });
  it('shows clinical compatibility gates without enabling their inputs', async () => {
    const { store, patch } = await setup({ slim: { label: 'SLiM', writable: false,
      inputs: ['weight'], compatibility_reason: 'Clinical equivalence review required.' } });
    render(<PatientFieldEditor store={store} name="slim" />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit SLiM inputs' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Clinical equivalence review required.');
    expect(screen.queryByDisplayValue('60')).not.toBeInTheDocument();
    expect(patch).not.toHaveBeenCalled();
  });
  it('fails closed on missing bindings and cycles, with a route-aware Your Info link', async () => {
    const { store } = await setup({ cycle: { label: 'Cycle', writable: false, inputs: ['cycle'] } });
    const view = render(<PatientFieldEditor store={store} name="missing" infoHref="/info/i/42" />);
    expect(screen.getByRole('link', { name: 'Open Your Info' })).toHaveAttribute('href', '/info/i/42');
    view.rerender(<PatientFieldEditor store={store} name="cycle" />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit Cycle inputs' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Circular field dependency');
  });
});
