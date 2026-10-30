import { act, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { AxiosInstance } from 'axios';
import PatientFieldEditor from './PatientFieldEditor';
import { PatientFormStore } from './patientFormStore';

async function setup(extraFields = {}, extraValues = {}, writeEnabled = true) {
  const fields = {
    height: { writable: true, target: 'patient_record', control: 'number', label: 'Height', unit: 'cm' },
    weight: { writable: true, target: 'patient_record', control: 'number', label: 'Weight', unit: 'kg' },
    bmi: { writable: false, kind: 'computed', label: 'BMI', inputs: ['height', 'weight'], bindings: ['bmiMin'] },
    ...extraFields,
  };
  const get = vi.fn(async (url: string) => ({ data: url.includes('writable-fields') ? fields : {
    patient_info: { person_id: 12, height: 170, weight: 60, bmi: 20.76,
      ldh_upper_limit_normal: 250, ...extraValues }, write_enabled: writeEnabled,
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
  it('gates the computed result while keeping its source inputs editable', async () => {
    const { store, patch } = await setup({ slim: { label: 'SLiM', writable: false,
      inputs: ['weight'], compatibility_reason: 'Clinical equivalence review required.',
      compatibility_scope: 'result' } });
    render(<PatientFieldEditor store={store} name="slim" />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit SLiM inputs' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Clinical equivalence review required.');
    expect(screen.getByRole('dialog')).toHaveTextContent('The result stays read-only');
    const weight = screen.getByDisplayValue('60');
    expect(weight).toBeEnabled();
    fireEvent.change(weight, { target: { value: '70' } });
    await act(async () => { await store.flush(); });
    expect(patch.mock.calls[0][1]).toEqual({ weight: 70 });
    expect(patch.mock.calls[0][1]).not.toHaveProperty('slim');
  });
  it('keeps DOB and ULN navigable and editable through nested MCL and MIPI gates', async () => {
    const { store, patch } = await setup({
      high_risk_mcl_criteria: { label: 'MCL', writable: false, inputs: ['mipi_risk'],
        compatibility_reason: 'MCL clinical review required.', compatibility_scope: 'result' },
      // No child gate here: the ancestor alone must not make source facts read-only.
      mipi_risk: { label: 'MIPI', writable: false, inputs: ['date_of_birth', 'ldh_upper_limit_normal'] },
      date_of_birth: { label: 'Date of birth', writable: true, target: 'patient_record', control: 'date' },
      ldh_upper_limit_normal: { label: 'LDH upper limit normal', writable: true, target: 'patient_record', control: 'number' },
    });
    render(<PatientFieldEditor store={store} name="high_risk_mcl_criteria" />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit MCL inputs' }));
    expect(screen.getByRole('alert')).toHaveTextContent('MCL clinical review required.');
    fireEvent.click(screen.getByRole('button', { name: 'Edit MIPI inputs' }));
    const dialog = screen.getByRole('dialog', { name: 'MIPI' });
    expect(within(dialog).getByText('Date of birth')).toBeVisible();
    const uln = within(dialog).getByDisplayValue('250');
    expect(uln).toBeEnabled();
    expect(dialog).not.toHaveTextContent('No reviewed PROMOP field binding');
    fireEvent.change(uln, { target: { value: '300' } });
    await act(async () => { await store.flush(); });
    expect(patch.mock.calls[0][1]).toEqual({ ldh_upper_limit_normal: 300 });
    expect(patch.mock.calls[0][1]).not.toHaveProperty('mipi_risk');
    expect(patch.mock.calls[0][1]).not.toHaveProperty('high_risk_mcl_criteria');
  });
  it('fails closed on missing bindings and cycles, with a route-aware Your Info link', async () => {
    const { store } = await setup({ cycle: { label: 'Cycle', writable: false, inputs: ['cycle'] } });
    const view = render(<PatientFieldEditor store={store} name="missing" infoHref="/info/i/42" />);
    expect(screen.getByRole('link', { name: 'Open Your Info' })).toHaveAttribute('href', '/info/i/42');
    view.rerender(<PatientFieldEditor store={store} name="cycle" />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit Cycle inputs' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Circular field dependency');
  });

  it('renders a computed value read-only when the host withholds its only source', async () => {
    // No Active Infection / No Other Active Malignancies depend on condition
    // facts this host does not expose; an empty "inputs below" dialog was the
    // reported bug (#5600).
    const { store } = await setup({ no_active_infection_status: {
      label: 'No active infection', writable: false, kind: 'computed',
      inputs: ['active_infection_status'],
      reason: 'Computed from active_infection_status.',
    } });
    render(<PatientFieldEditor store={store} name="no_active_infection_status" />);
    expect(screen.queryByRole('button', { name: /Edit .* inputs/ })).not.toBeInTheDocument();
    expect(screen.getByText(/not editable in this view/)).toBeInTheDocument();
    expect(screen.queryByText('No reviewed PROMOP field binding')).not.toBeInTheDocument();
  });

  it('does not offer a partial dialog when any dependency is withheld', async () => {
    const { store } = await setup({ composite_partial: {
      label: 'Composite partial', writable: false, inputs: ['weight', 'active_malignancies'],
    } });
    render(<PatientFieldEditor store={store} name="composite_partial" />);
    expect(screen.queryByRole('button', { name: 'Edit Composite partial inputs' })).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

describe('current-diagnosis tri-state fields (#5521)', () => {
  it('offers Unknown as a third answer and saves it as null', async () => {
    const user = userEvent.setup();
    const { store, patch } = await setup({
      smoldering_multiple_myeloma: {
        label: 'Smoldering Multiple Myeloma', writable: true, target: 'patient_record',
        control: 'boolean', value_kind: 'boolean', tri_state: true,
        bindings: ['smolderingMultipleMyeloma'],
      },
    }, { smoldering_multiple_myeloma: false });
    render(<PatientFieldEditor store={store} name="smolderingMultipleMyeloma" />);

    expect(screen.getByRole('combobox')).toHaveTextContent('No');

    await user.click(screen.getByRole('combobox'));
    await user.click(screen.getByRole('option', { name: 'Unknown' }));
    await act(async () => { await store.flush(); });

    expect(patch.mock.calls[0][1]).toEqual({ smoldering_multiple_myeloma: null });
  });
});

it('uses stored and pending DOB precision in the shared date editor', async () => {
  const { store, patch } = await setup({
    date_of_birth: { label: 'Date of birth', writable: true, target: 'patient_record', control: 'date' },
    date_of_birth_precision: { writable: true, target: 'patient_record', control: 'text' },
  }, { date_of_birth: '1985-01-01', date_of_birth_precision: 'year' });
  render(<PatientFieldEditor store={store} name="date_of_birth" />);
  expect(screen.getByRole('spinbutton')).toHaveValue(1985);
  fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '1986' } });
  await act(async () => { await store.flush(); });
  expect(patch.mock.calls[0][1]).toEqual({ date_of_birth: '1986-01-01' });
});

describe.each(['readOnly', 'unwritableStore', 'unwritableField'] as const)('DOB precision in %s views', (mode) => {
  it.each([
    ['year', '1985-01-01', '1985'],
    ['month', '1985-06-01', '1985-06'],
    ['day', '1985-06-15', '1985-06-15'],
    ['year', null, 'Unknown'],
  ])('shows only known components for %s precision', async (precision, value, expected) => {
    const { store } = await setup({
      date_of_birth: { label: 'Date of birth', writable: mode !== 'unwritableField', target: 'patient_record', control: 'date' },
    }, { date_of_birth: value, date_of_birth_precision: precision }, mode !== 'unwritableStore');
    const { container } = render(<PatientFieldEditor store={store} name="date_of_birth" readOnly={mode === 'readOnly'} />);
    expect(container.querySelector('[data-field="date_of_birth"] > div')?.textContent).toBe(`Date of birth: ${expected}`);
    if (precision !== 'day' && value) expect(container).not.toHaveTextContent(value);
    expect(screen.queryByRole('spinbutton')).not.toBeInTheDocument();
  });
});
