import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
  return { store, patch, get };
}

describe('computed patient field input dialogs', () => {
  it.each([
    ['hiv_status', 'Hiv status', 'HIV Positive'],
    ['hepatitis_b_status', 'Hepatitis b status', 'Hepatitis B Positive'],
    ['hepatitis_c_status', 'Hepatitis c status', 'Hepatitis C Positive'],
  ])('uses the caller label for %s instead of the generated descriptor label', async (name, generatedLabel, callerLabel) => {
    const { store } = await setup({ [name]: {
      writable: false, label: generatedLabel, value_kind: 'boolean', reason: 'Computed status.',
    } });
    render(<PatientFieldEditor store={store} name={name} label={callerLabel} />);

    expect(screen.getByText(`${callerLabel}:`)).toBeVisible();
    expect(screen.queryByText(`${generatedLabel}:`)).not.toBeInTheDocument();
  });

  it('offers both reviewed answers for a nullable boolean (#5601)', async () => {
    const user = userEvent.setup();
    const { store, patch } = await setup({
      hiv_status: {
        writable: true, target: 'patient_record', control: 'boolean',
        label: 'HIV Positive', value_kind: 'boolean',
        answer_labels: { true: 'HIV Positive', false: 'No HIV' },
      },
    }, { hiv_status: null });
    render(<PatientFieldEditor store={store} name="hiv_status" label="HIV Positive" />);

    await user.click(screen.getByRole('combobox'));
    expect(screen.getByRole('option', { name: 'HIV Positive' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Yes' })).not.toBeInTheDocument();
    // The negative answer is `false` on the canonical column, not a second field.
    await user.click(screen.getByRole('option', { name: 'No HIV' }));
    await act(async () => { await store.flush(); });
    const [url, body] = patch.mock.calls.at(-1) ?? [];
    expect(String(url)).toContain('patient-records');
    expect(body).toEqual({ hiv_status: false });
  });

  it('reads a negative answer with its reviewed wording', async () => {
    const { store } = await setup({
      hiv_status: {
        writable: false, label: 'HIV Positive', value_kind: 'boolean',
        answer_labels: { true: 'HIV Positive', false: 'No HIV' },
        reason: 'Read-only.',
      },
    }, { hiv_status: false });
    render(<PatientFieldEditor store={store} name="hiv_status" label="HIV Positive" />);

    expect(screen.getByText(/No HIV/)).toBeVisible();
    expect(screen.queryByText(/No:/)).not.toBeInTheDocument();
  });

  it('keeps the descriptor label when the caller has no display label', async () => {
    const { store } = await setup({ field_without_caller_label: {
      writable: false, label: 'Descriptor label', value_kind: 'string', reason: 'Read-only.',
    } });
    render(<PatientFieldEditor store={store} name="field_without_caller_label" />);

    expect(screen.getByText('Descriptor label:')).toBeVisible();
  });

  it('opens editable inputs and changes the server-computed result in both displays', async () => {
    const { store, patch } = await setup();
    render(<><PatientFieldEditor store={store} name="bmiMin" /><PatientFieldEditor store={store} name="bmi" /></>);
    fireEvent.click(screen.getAllByRole('button', { name: 'Edit BMI inputs' })[0]);
    const dialog = screen.getByRole('dialog');
    // Centred without translate utilities, which a Tailwind 3 host doubles.
    expect(dialog).toHaveClass('inset-0', 'm-auto', 'h-fit');
    expect(dialog).not.toHaveClass('-translate-x-1/2', '-translate-y-1/2');
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

  it('does not offer the inputs pencil when no dependency can be edited (#2244)', async () => {
    // No Active Infection resolves to active_infection_status, itself computed
    // from OMOP condition facts with no PatientRecord column. Both dependencies
    // are present, so the old availability check kept the pencil and opened a
    // dialog with a value and no control.
    const { store } = await setup({
      no_active_infection_status: {
        label: 'No Active Infection', writable: false, kind: 'computed',
        inputs: ['active_infection_status'],
        reason: 'Computed from active_infection_status.',
      },
      active_infection_status: {
        label: 'Active Infection', writable: false, kind: 'computed', inputs: [],
        reason: 'Computed from OMOP facts; there is no record field to edit.',
      },
    }, { no_active_infection_status: null, active_infection_status: null });
    render(<PatientFieldEditor store={store} name="no_active_infection_status" />);

    expect(screen.queryByRole('button', { name: /Edit .* inputs/ })).not.toBeInTheDocument();
    expect(screen.getByText('Unknown')).toBeVisible();
    expect(screen.getByText(/not editable in this view/)).toBeInTheDocument();
  });

  it('walks past the first dependency before promising an edit path', async () => {
    const { store } = await setup({
      outer: { label: 'Outer', writable: false, inputs: ['middle'] },
      middle: { label: 'Middle', writable: false, inputs: ['inner'] },
      inner: {
        label: 'Inner', writable: false, kind: 'computed', inputs: [],
        reason: 'Computed from OMOP facts; there is no record field to edit.',
      },
    });
    render(<PatientFieldEditor store={store} name="outer" />);

    expect(screen.queryByRole('button', { name: 'Edit Outer inputs' })).not.toBeInTheDocument();
  });

  it.each([
    ['derived first', ['active_infection_status', 'weight']],
    ['editable first', ['weight', 'active_infection_status']],
  ])('keeps the pencil when another dependency is editable (%s)', async (_order, inputs) => {
    const { store } = await setup({
      mixed: { label: 'Mixed', writable: false, inputs },
      active_infection_status: {
        label: 'Active Infection', writable: false, kind: 'computed', inputs: [],
        reason: 'Computed from OMOP facts; there is no record field to edit.',
      },
    });
    render(<PatientFieldEditor store={store} name="mixed" />);

    fireEvent.click(screen.getByRole('button', { name: 'Edit Mixed inputs' }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByDisplayValue('60')).toBeEnabled();
    expect(within(dialog).getByText(/no record field to edit/)).toBeInTheDocument();
  });

  it('follows an alias dependency to its editable canonical', async () => {
    const { store } = await setup({
      outer_alias: { label: 'Outer alias', writable: false, inputs: ['bmi_alias'] },
      bmi_alias: { label: 'BMI alias', writable: false, canonical: 'bmi' },
    });
    render(<PatientFieldEditor store={store} name="outer_alias" />);

    fireEvent.click(screen.getByRole('button', { name: 'Edit Outer alias inputs' }));
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: 'Edit BMI alias inputs' }))
      .toBeInTheDocument();
  });

  it('does not count a branch the dialog would refuse to open', async () => {
    // `inner` has one editable input and one withheld one, so its own dialog
    // never opens (#5600). Its writable descendant is unreachable from `outer`.
    const { store } = await setup({
      outer_mixed: { label: 'Outer mixed', writable: false, inputs: ['inner_mixed'] },
      inner_mixed: { label: 'Inner mixed', writable: false, inputs: ['weight', 'missing_source'] },
    });
    render(<PatientFieldEditor store={store} name="outer_mixed" />);

    expect(screen.queryByRole('button', { name: 'Edit Outer mixed inputs' })).not.toBeInTheDocument();
  });

  it.each([
    ['a derived leaf', 'derived_leaf'],
    ['a withheld leaf', 'missing_source'],
  ])('falls through to a direct control when the dialog has no edit path (%s)', async (_case, input) => {
    // A field may be directly writable and still declare inputs. When those
    // inputs cannot be edited, the row itself renders its control; the walk
    // must not stop at the unopenable dialog.
    const { store } = await setup({
      outer_direct: { label: 'Outer direct', writable: false, inputs: ['direct_with_inputs'] },
      direct_with_inputs: {
        label: 'Direct with inputs', writable: true, target: 'patient_record',
        control: 'text', inputs: [input],
      },
      derived_leaf: {
        label: 'Derived leaf', writable: false, kind: 'computed', inputs: [],
        reason: 'Computed from OMOP facts; there is no record field to edit.',
      },
    });
    render(<PatientFieldEditor store={store} name="outer_direct" />);

    fireEvent.click(screen.getByRole('button', { name: 'Edit Outer direct inputs' }));
    expect(within(screen.getByRole('dialog')).getByRole('textbox')).toBeEnabled();
  });

  it('keeps the gate reason when a gated row loses its dialog', async () => {
    const { store } = await setup({
      gated_derived: {
        label: 'Gated derived', writable: false, inputs: ['active_infection_status'],
        compatibility_reason: 'Clinical equivalence review required.',
      },
      active_infection_status: {
        label: 'Active Infection', writable: false, kind: 'computed', inputs: [],
        reason: 'Computed from OMOP facts; there is no record field to edit.',
      },
    });
    render(<PatientFieldEditor store={store} name="gated_derived" />);

    expect(screen.queryByRole('button', { name: /Edit .* inputs/ })).not.toBeInTheDocument();
    expect(screen.getByText(/not editable in this view/)).toBeVisible();
    expect(screen.getByText(/Clinical equivalence review required/)).toBeVisible();
  });

  it('edits a directly writable alias in place instead of drilling into its canonical', async () => {
    // Refractory Status is writable directly but also carries `canonical`, so
    // the alias fallback opened a one-row dialog and hid the select (#5605).
    const { store, patch } = await setup({
      refractory_status: { label: 'Refractory Status', writable: true, target: 'patient_record',
        value_kind: 'string', canonical: 'treatment_refractory_status',
        options: [{ value: 'Unknown' }, { value: 'Primary Refractory' }] },
      treatment_refractory_status: { label: 'Treatment refractory status', writable: true,
        target: 'patient_record', value_kind: 'string',
        options: [{ value: 'Unknown' }, { value: 'Primary Refractory' }] },
    }, { refractory_status: 'Unknown' });
    render(<PatientFieldEditor store={store} name="refractory_status" />);

    expect(screen.queryByRole('button', { name: /Edit Refractory Status inputs/ })).not.toBeInTheDocument();
    expect(screen.getByRole('combobox')).toHaveTextContent('Unknown');

    await userEvent.click(screen.getByRole('combobox'));
    await userEvent.click(await screen.findByText('Primary Refractory'));
    await act(async () => { await store.flush(); });

    expect(patch.mock.calls[0][1]).toEqual({ refractory_status: 'Primary Refractory' });
  });
});

describe('read-only views do not call a stored field derived', () => {
  it.each(['readOnly', 'unwritableStore'] as const)('shows a writable field plainly in a %s view', async (mode) => {
    const { store } = await setup({}, {}, mode !== 'unwritableStore');
    const { container } = render(
      <PatientFieldEditor store={store} name="weight" readOnly={mode === 'readOnly'} />);

    expect(container.querySelector('[data-field="weight"] > div')?.textContent).toBe('Weight: 60');
    expect(container).not.toHaveTextContent(/derived/i);
    expect(screen.queryByRole('spinbutton')).not.toBeInTheDocument();
  });

  it('keeps the descriptor reason for a field locked in a read-only view', async () => {
    const { store } = await setup({
      weight: { writable: true, target: 'patient_record', control: 'number', label: 'Weight',
        reason: 'Recorded by the care team.' },
    });
    render(<PatientFieldEditor store={store} name="weight" readOnly />);

    expect(screen.getByText('Recorded by the care team.')).toBeVisible();
    expect(screen.queryByText(/derived/i)).not.toBeInTheDocument();
  });

  it('still explains a field the record does not accept', async () => {
    const { store } = await setup({
      ldh_upper_limit_normal: { writable: false, label: 'LDH ULN' },
    });
    render(<PatientFieldEditor store={store} name="ldh_upper_limit_normal" readOnly />);

    expect(screen.getByText('Derived value; edit the source inputs.')).toBeVisible();
  });
});

describe('tab choices for a descriptor without options', () => {
  it('renders a select from the tab choices instead of free text', async () => {
    const user = userEvent.setup();
    const { store, patch } = await setup({
      post_transformation_outcome: { writable: true, target: 'patient_record',
        value_kind: 'string', label: 'Post-Transformation Outcome' },
    });
    render(<PatientFieldEditor store={store} name="post_transformation_outcome"
      label="Post-Transformation Outcome" options={['Complete response', 'Progression']} />);

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    await user.click(screen.getByRole('combobox'));
    await user.click(screen.getByRole('option', { name: 'Progression' }));
    await act(async () => { await store.flush(); });
    expect(patch.mock.calls.at(-1)?.[1]).toEqual({ post_transformation_outcome: 'Progression' });
  });

  it('prefers the descriptor options', async () => {
    const user = userEvent.setup();
    const { store } = await setup({
      smoking_status: { writable: true, target: 'patient_record', label: 'Smoking',
        options: [{ value: 'Never smoker' }] },
    });
    render(<PatientFieldEditor store={store} name="smoking_status" options={['Local only']} />);

    await user.click(screen.getByRole('combobox'));
    expect(screen.getByRole('option', { name: 'Never smoker' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Local only' })).not.toBeInTheDocument();
  });

  it('keeps a typed boolean or number control', async () => {
    const user = userEvent.setup();
    const { store, patch } = await setup({
      bulky_disease: { writable: true, target: 'patient_record', value_kind: 'boolean', label: 'Bulky' },
    });
    render(<>
      <PatientFieldEditor store={store} name="bulky_disease" options={['Yes', 'No']} />
      <PatientFieldEditor store={store} name="weight" options={['Yes', 'No']} />
    </>);

    expect(screen.getByRole('spinbutton', { name: 'Weight' })).toBeInTheDocument();
    await user.click(screen.getByRole('combobox'));
    await user.click(screen.getByRole('option', { name: 'Yes' }));
    await act(async () => { await store.flush(); });
    // The boolean column receives a boolean, not the tab's "Yes" string.
    expect(patch.mock.calls.at(-1)?.[1]).toEqual({ bulky_disease: true });
  });
});

describe('a tab that declares its own closed answer set', () => {
  it('renders a numeric scale as the tab picker and still writes a number', async () => {
    const user = userEvent.setup();
    const { store, patch } = await setup({
      ecog_performance_status: { writable: true, target: 'patient_record',
        control: 'number', value_kind: 'number', label: 'ECOG Performance Status' },
    }, { ecog_performance_status: 2 });
    render(<PatientFieldEditor store={store} name="ecog_performance_status"
      label="ECOG Performance Status" controlType="select"
      options={['0', '1', '2', '3', '4', '5']} />);

    // A number box would accept values the scale does not have.
    expect(screen.queryByRole('spinbutton')).not.toBeInTheDocument();
    await user.click(screen.getByRole('combobox'));
    await user.click(screen.getByRole('option', { name: '4' }));
    await act(async () => { await store.flush(); });
    // The numeric column receives a number, not the option's "4" string.
    expect(patch.mock.calls.at(-1)?.[1]).toEqual({ ecog_performance_status: 4 });
  });

  it('clears a numeric picker to null, not an empty string', async () => {
    const user = userEvent.setup();
    const { store, patch } = await setup({
      ecog_performance_status: { writable: true, target: 'patient_record',
        control: 'number', value_kind: 'number', label: 'ECOG Performance Status' },
    }, { ecog_performance_status: 2 });
    render(<PatientFieldEditor store={store} name="ecog_performance_status"
      label="ECOG Performance Status" controlType="select"
      options={['0', '1', '2']} />);

    await user.click(screen.getByRole('combobox'));
    await user.click(screen.getByRole('option', { name: '— None —' }));
    await act(async () => { await store.flush(); });
    expect(patch.mock.calls.at(-1)?.[1]).toEqual({ ecog_performance_status: null });
  });

  it('does not erase a numeric answer when the choice is not a number', async () => {
    const user = userEvent.setup();
    const { store, patch } = await setup({
      ecog_performance_status: { writable: true, target: 'patient_record',
        control: 'number', value_kind: 'number', label: 'ECOG Performance Status' },
    }, { ecog_performance_status: 2 });
    render(<PatientFieldEditor store={store} name="ecog_performance_status"
      label="ECOG Performance Status" controlType="select"
      options={['0', '1', 'Unknown']} />);

    await user.click(screen.getByRole('combobox'));
    await user.click(screen.getByRole('option', { name: 'Unknown' }));
    await act(async () => { await store.flush(); });
    // Sent as-is so the server refuses it visibly; null would silently clear.
    expect(patch.mock.calls.at(-1)?.[1]).toEqual({ ecog_performance_status: 'Unknown' });
  });

  it('keeps the declared email input type', async () => {
    const { store } = await setup({
      email: { writable: true, target: 'patient_record',
        control: 'text', value_kind: 'string', label: 'Email' },
    }, { email: 'patient@example.com' });
    const { container } = render(<PatientFieldEditor store={store} name="email"
      label="Email" controlType="email" />);

    expect(container.querySelector('input')?.getAttribute('type')).toBe('email');
  });

  it('renders a declared multiselect from the tab answers', async () => {
    const { store } = await setup({
      protein_expressions: { writable: true, target: 'patient_record',
        control: 'text', value_kind: 'string', label: 'Protein Expressions' },
    }, { protein_expressions: 'CD5,CD23' });
    render(<PatientFieldEditor store={store} name="protein_expressions"
      label="Protein Expressions" controlType="multiselect"
      options={['CD5', 'CD10', 'CD23']} />);

    // A single select would show one answer and write over the rest.
    expect(screen.getByRole('button', { name: /CD5, CD23/ })).toBeInTheDocument();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('never turns a date into a list of strings', async () => {
    const { store } = await setup({
      diagnosis_date: { writable: true, target: 'patient_record',
        control: 'date', value_kind: 'date', label: 'Diagnosis Date' },
    }, { diagnosis_date: '2025-01-02' });
    const { container } = render(<PatientFieldEditor store={store} name="diagnosis_date"
      label="Diagnosis Date" controlType="select" options={['2025-01-02']} />);

    expect(container.querySelector('input')?.getAttribute('type')).toBe('date');
  });
});

describe('read-only multiple answers', () => {
  it('lists an array value by its labels instead of JSON', async () => {
    const { store } = await setup({
      planned_therapies: { writable: false, label: 'Planned therapies', multiple: true,
        reason: 'Read-only.', options: [{ value: 'r_chop', label: 'R-CHOP' }] },
    }, { planned_therapies: ['r_chop', 'Local regimen'] });
    const { container } = render(<PatientFieldEditor store={store} name="planned_therapies" />);

    expect(container.querySelector('[data-field="planned_therapies"] > div')?.textContent)
      .toBe('Planned therapies: R-CHOP, Local regimen');
    expect(container).not.toHaveTextContent('[');
  });

  it('keeps a list of records serialised instead of [object Object]', async () => {
    const { store } = await setup({
      lines_of_therapy: { writable: false, label: 'Lines', reason: 'Read-only.' },
    }, { lines_of_therapy: [{ line: 1, regimen: 'R-CHOP' }] });
    const { container } = render(<PatientFieldEditor store={store} name="lines_of_therapy" />);

    expect(container).not.toHaveTextContent('[object Object]');
    expect(container).toHaveTextContent('R-CHOP');
  });

  it('does not list a null entry as "null"', async () => {
    const { store } = await setup({
      planned_therapies: { writable: false, label: 'Planned therapies', multiple: true,
        reason: 'Read-only.' },
    }, { planned_therapies: [null, 'r_chop'] });
    const { container } = render(<PatientFieldEditor store={store} name="planned_therapies" />);

    expect(container.querySelector('[data-field="planned_therapies"] > div')?.textContent)
      .toBe('Planned therapies: r_chop');
    expect(container).not.toHaveTextContent('null');
  });

  it('splits a comma-joined multiple answer', async () => {
    const { store } = await setup({
      stem_cell_transplant_history: { writable: true, target: 'patient_record', multiple: true,
        label: 'SCT history' },
    }, { stem_cell_transplant_history: 'autologous SCT,tandem SCT' });
    const { container } = render(
      <PatientFieldEditor store={store} name="stem_cell_transplant_history" readOnly />);

    expect(container.querySelector('[data-field="stem_cell_transplant_history"] > div')?.textContent)
      .toBe('SCT history: autologous SCT, tandem SCT');
  });
});

describe('a failed save', () => {
  it('keeps the draft in the field and retries it from the row', async () => {
    const { store, patch } = await setup();
    patch.mockRejectedValueOnce(new Error('offline'));
    render(<PatientFieldEditor store={store} name="weight" />);

    fireEvent.change(screen.getByRole('spinbutton', { name: 'Weight' }), { target: { value: '80' } });
    await act(async () => { await store.flush(); });

    expect(screen.getByRole('spinbutton', { name: 'Weight' })).toHaveValue(80);
    expect(screen.getByRole('alert')).toHaveTextContent('Retry to send it again');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await act(async () => { await store.flush(); });

    expect(patch).toHaveBeenCalledTimes(2);
    expect(patch.mock.calls[1][1]).toEqual({ weight: 80 });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('negated trial questions (#5663)', () => {
  const hivStatus = {
    label: 'HIV Positive', writable: true, target: 'patient_record',
    control: 'boolean', value_kind: 'boolean' as const,
  };
  const noHiv = {
    label: 'No HIV', kind: 'negated' as const, writable: false,
    canonical: 'hiv_status', inverted: true, value_kind: 'boolean' as const,
    reason: 'Inverted view of hiv_status: the record stores the positive status.',
  };

  it('shows the negated question and writes the positive column inverted', async () => {
    const user = userEvent.setup();
    const { store, patch } = await setup({ hiv_status: hivStatus, no_hiv_status: noHiv },
      { hiv_status: false });
    render(<PatientFieldEditor store={store} name="no_hiv_status" label="No HIV" />);

    // `hiv_status: false` answers "No HIV" with Yes.
    expect(screen.getByRole('combobox')).toHaveTextContent('Yes');

    await user.click(screen.getByRole('combobox'));
    await user.click(screen.getByRole('option', { name: 'No' }));
    await act(async () => { await store.flush(); });

    // A write to `no_hiv_status` would invent a column; the answer lands on the
    // positive field, inverted.
    expect(patch.mock.calls.at(-1)?.[1]).toEqual({ hiv_status: true });
  });

  it('writes the opposite answer too', async () => {
    const user = userEvent.setup();
    const { store, patch } = await setup({ hiv_status: hivStatus, no_hiv_status: noHiv },
      { hiv_status: true });
    render(<PatientFieldEditor store={store} name="no_hiv_status" label="No HIV" />);

    // `hiv_status: true` answers "No HIV" with No.
    expect(screen.getByRole('combobox')).toHaveTextContent('No');

    await user.click(screen.getByRole('combobox'));
    await user.click(screen.getByRole('option', { name: 'Yes' }));
    await act(async () => { await store.flush(); });

    expect(patch.mock.calls.at(-1)?.[1]).toEqual({ hiv_status: false });
  });

  it('keeps Unknown as null for a tri-state positive column', async () => {
    const user = userEvent.setup();
    const { store, patch } = await setup({
      hiv_status: { ...hivStatus, tri_state: true },
      no_hiv_status: noHiv,
    }, { hiv_status: true });
    render(<PatientFieldEditor store={store} name="no_hiv_status" label="No HIV" />);

    expect(screen.getByRole('combobox')).toHaveTextContent('No');

    await user.click(screen.getByRole('combobox'));
    await user.click(screen.getByRole('option', { name: 'Unknown' }));
    await act(async () => { await store.flush(); });

    expect(patch.mock.calls.at(-1)?.[1]).toEqual({ hiv_status: null });
  });

  it('stays read-only when the positive column is not writable', async () => {
    const { store } = await setup({
      hiv_status: { ...hivStatus, writable: false, reason: 'Withheld by the host.' },
      no_hiv_status: noHiv,
    }, { hiv_status: false });
    const { container } = render(
      <PatientFieldEditor store={store} name="no_hiv_status" label="No HIV" />);

    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(container).toHaveTextContent('No HIV: Yes');
    // Both halves stay on screen: why it is read-only, and how the value maps.
    expect(screen.getByText(/Withheld by the host/)).toBeVisible();
    expect(screen.getByText(/Inverted view of hiv_status/)).toBeVisible();
  });

  it('reads an unanswered negated view as Unknown', async () => {
    const { store } = await setup({ hiv_status: hivStatus, no_hiv_status: noHiv },
      { hiv_status: null });
    const { container } = render(
      <PatientFieldEditor store={store} name="no_hiv_status" label="No HIV" readOnly />);

    expect(container).toHaveTextContent('No HIV: Unknown');
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('falls back to a read-only row when the positive column has no descriptor', async () => {
    const { store } = await setup({ no_hiv_status: noHiv }, { hiv_status: false });
    const { container } = render(
      <PatientFieldEditor store={store} name="no_hiv_status" label="No HIV" />);

    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(container).toHaveTextContent('No HIV: Unknown');
    expect(screen.getByText(/not editable in this view/)).toBeVisible();
  });
});

describe('computed value dialog copy', () => {
  it('describes the dependency without promising a save step', async () => {
    const user = userEvent.setup();
    const { store } = await setup();
    render(<PatientFieldEditor store={store} name="bmi" />);

    await user.click(screen.getByRole('button', { name: /Edit BMI inputs/ }));

    expect(screen.getByText(/This result is recalculated from them/)).toBeVisible();
    expect(screen.queryByText(/Save their values/)).not.toBeInTheDocument();
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

describe('host-labelled options', () => {
  it('shows the host label and writes the host value', async () => {
    const { store, patch } = await setup({
      planned_therapies: { label: 'Planned Therapies', writable: true, target: 'patient_record',
        multiple: true, options: [{ value: 'r_chop', label: 'R-CHOP' }] },
    }, { planned_therapies: '' });
    render(<PatientFieldEditor store={store} name="planned_therapies" />);

    // The control is named by the field label plus the current selection.
    await userEvent.click(screen.getByRole('button', { name: /Planned Therapies/ }));
    await userEvent.click(await screen.findByText('R-CHOP'));
    await act(async () => { await store.flush(); });

    expect(patch.mock.calls[0][1]).toEqual({ planned_therapies: 'r_chop' });
    expect(screen.getByRole('button', { name: 'Planned Therapies: R-CHOP' }))
      .toBeInTheDocument();
  });

  it('names a compact control by the caller label', async () => {
    const { store } = await setup();
    render(<PatientFieldEditor store={store} name="height" compact label="Height at intake" />);

    // Compact mounts hide the visible label; the control still needs a name.
    expect(screen.getByRole('spinbutton', { name: 'Height at intake' })).toBeInTheDocument();
  });

  it('explains an empty catalog instead of rendering a bare picker', async () => {
    const { store } = await setup({
      planned_therapies: { label: 'Planned Therapies', writable: true, target: 'patient_record',
        multiple: true, options: [],
        options_note: 'No planned therapies are available for this disease and treatment line.' },
    });
    render(<PatientFieldEditor store={store} name="planned_therapies" />);

    expect(screen.getByRole('status'))
      .toHaveTextContent('No planned therapies are available');
  });

  it('retries a failed catalog load', async () => {
    const { store, get } = await setup({
      planned_therapies: { label: 'Planned Therapies', writable: false, retryable: true,
        reason: 'Could not load planned therapies.' },
    });
    render(<PatientFieldEditor store={store} name="planned_therapies" />);
    const calls = get.mock.calls.length;

    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(get.mock.calls.length).toBeGreaterThan(calls);
  });
});

describe('dated lab results', () => {
  it('edits the value and its result date together', async () => {
    const { store, patch } = await setup({
      hemoglobin_g_dl: { label: 'Hemoglobin', writable: true, target: 'patient_record',
        control: 'number', dated: true, latest_date: '2026-08-01',
        projection: { omop_table: 'measurement', unit: 'g/dL' } },
    }, { hemoglobin_g_dl: 12.5 });
    render(<PatientFieldEditor store={store} name="hemoglobin_g_dl" />);

    expect(screen.getByLabelText('Result date')).toHaveValue('2026-08-01');
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '13' } });
    fireEvent.change(screen.getByLabelText('Result date'), { target: { value: '2026-08-02' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save result' }));
    await act(async () => { await store.flush(); });

    expect(patch.mock.calls[0][1])
      .toEqual({ hemoglobin_g_dl: { value: 13, date: '2026-08-02' } });
  });

  it('asks for the correction date when the retained value was edited before', async () => {
    // The record may keep an older pair while a newer fact exists. Defaulting
    // to the newest date would file the correction on the wrong result.
    const { store, patch } = await setup({
      hemoglobin_g_dl: { label: 'Hemoglobin', writable: true, target: 'patient_record',
        control: 'number', dated: true, latest_date: '2026-08-01', user_edited: true,
        projection: { omop_table: 'measurement' } },
    }, { hemoglobin_g_dl: 12.5 });
    render(<PatientFieldEditor store={store} name="hemoglobin_g_dl" />);

    expect(screen.getByLabelText('Result date')).toHaveValue('');
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '13' } });
    expect(screen.getByRole('button', { name: 'Save result' })).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Result date'), { target: { value: '2026-07-15' } });
    expect(screen.getByRole('button', { name: 'Save result' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Save result' }));
    await act(async () => { await store.flush(); });

    expect(patch.mock.calls[0][1])
      .toEqual({ hemoglobin_g_dl: { value: 13, date: '2026-07-15' } });
  });

  it('asks for a date even when the edited pair has no projected fact', async () => {
    const { store } = await setup({
      hemoglobin_g_dl: { label: 'Hemoglobin', writable: true, target: 'patient_record',
        control: 'number', dated: true, user_edited: true },
    }, { hemoglobin_g_dl: 12.5 });
    render(<PatientFieldEditor store={store} name="hemoglobin_g_dl" />);

    // No `latest_date` to fall back to: the old code would have used today.
    expect(screen.getByLabelText('Result date')).toHaveValue('');
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '13' } });
    expect(screen.getByRole('button', { name: 'Save result' })).toBeDisabled();
  });

  it('does not save a dated value before the pair is committed', async () => {
    const { store, patch } = await setup({
      hemoglobin_g_dl: { label: 'Hemoglobin', writable: true, target: 'patient_record',
        control: 'number', dated: true, latest_date: '2026-08-01',
        projection: { omop_table: 'measurement' } },
    }, { hemoglobin_g_dl: 12.5 });
    render(<PatientFieldEditor store={store} name="hemoglobin_g_dl" />);

    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '13' } });
    await act(async () => { await store.flush(); });

    expect(patch).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Save result' })).toBeEnabled();
  });

  it('keeps showing the pending result date until the save is acknowledged', async () => {
    const { store } = await setup({
      hemoglobin_g_dl: { label: 'Hemoglobin', writable: true, target: 'patient_record',
        control: 'number', dated: true, latest_date: '2026-08-01',
        projection: { omop_table: 'measurement' } },
    }, { hemoglobin_g_dl: 12.5 });
    render(<PatientFieldEditor store={store} name="hemoglobin_g_dl" />);

    act(() => store.enqueue('hemoglobin_g_dl', { value: 13, date: '2026-08-03' }));

    expect(screen.getByLabelText('Result date')).toHaveValue('2026-08-03');
  });

  it('does not write a date change before Save result', async () => {
    const { store, patch } = await setup({
      hemoglobin_g_dl: { label: 'Hemoglobin', writable: true, target: 'patient_record',
        control: 'number', dated: true, latest_date: '2026-08-01',
        projection: { omop_table: 'measurement' } },
    }, { hemoglobin_g_dl: 12.5 });
    render(<PatientFieldEditor store={store} name="hemoglobin_g_dl" />);

    fireEvent.change(screen.getByLabelText('Result date'), { target: { value: '2026-07-01' } });
    await act(async () => { await store.flush(); });

    expect(patch).not.toHaveBeenCalled();
  });

  it('does not promise that an older projected result stays out of the current value', async () => {
    const { store } = await setup({
      hemoglobin_g_dl: { label: 'Hemoglobin', writable: true, target: 'patient_record',
        control: 'number', dated: true, latest_date: '2026-08-01',
        projection: { omop_table: 'measurement' } },
    }, { hemoglobin_g_dl: 12.5 });
    render(<PatientFieldEditor store={store} name="hemoglobin_g_dl" />);

    fireEvent.change(screen.getByLabelText('Result date'), { target: { value: '2026-07-01' } });

    const status = screen.getByRole('status');
    expect(status).toHaveTextContent('may show as the current value');
    expect(status).toHaveTextContent('newer result then becomes current again');
    expect(status).not.toHaveTextContent(/not as the current value/);
  });

  it('does not promise history for a dated source pair', async () => {
    const { store } = await setup({
      hemoglobin_level: { label: 'Hemoglobin', writable: true,
        target: 'patient_record', control: 'number', dated: true,
        latest_date: '2026-08-01' },
    }, { hemoglobin_level: 12.5 });
    render(<PatientFieldEditor store={store} name="hemoglobin_level" />);

    fireEvent.change(screen.getByLabelText('Result date'), { target: { value: '2026-07-01' } });

    const status = screen.getByRole('status');
    expect(status).toHaveTextContent('stays the current value');
    expect(status).not.toHaveTextContent('stored as history');
  });

  it('blocks the next dated save when the descriptor refresh failed, until Retry', async () => {
    const fields = { hemoglobin_g_dl: { label: 'Hemoglobin', writable: true,
      target: 'patient_record', control: 'number', dated: true, latest_date: '2026-08-01',
      projection: { omop_table: 'measurement' } } };
    let metadataFails = false;
    const get = vi.fn(async (url: string) => {
      if (url.includes('writable-fields')) {
        if (metadataFails) throw new Error('metadata unavailable');
        return { data: fields };
      }
      return { data: { patient_info: { person_id: 12, hemoglobin_g_dl: 12.5 }, write_enabled: true } };
    });
    const patch = vi.fn(async (_url: string, values: Record<string, unknown>) => ({ data: {
      patient_info: { person_id: 12, hemoglobin_g_dl: 13 }, write: { applied: Object.keys(values) },
    } }));
    const store = new PatientFormStore({ get, patch } as unknown as AxiosInstance, '/api');
    await store.load();
    render(<PatientFieldEditor store={store} name="hemoglobin_g_dl" />);

    metadataFails = true;
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '13' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save result' }));
    await act(async () => { await store.flush(); });

    expect(screen.getByText(/correction target may be out of date/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Save result' })).toBeDisabled();

    metadataFails = false;
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Retry' })); });
    await waitFor(() => expect(
      screen.queryByText(/correction target may be out of date/)).not.toBeInTheDocument());

    // The refreshed target is usable again, but only after the patient commits
    // a pair for it: nothing was re-sent on their behalf.
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '14' } });
    expect(screen.getByRole('button', { name: 'Save result' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Save result' }));
    await act(async () => { await store.flush(); });

    expect(patch).toHaveBeenCalledTimes(2);
    expect(patch.mock.calls[1][1])
      .toEqual({ hemoglobin_g_dl: { value: 14, date: '2026-08-01' } });
  });
});

describe('single unit display (#5704)', () => {
  function unitRow(extra: Record<string, unknown> = {}) {
    return {
      hemoglobin_g_dl: {
        writable: true, target: 'patient_record', control: 'number',
        label: 'Hemoglobin', unit: 'g/dL', ...extra,
      },
    };
  }

  it('does not append the unit when the caller label already carries it', async () => {
    const { store } = await setup(unitRow(), { hemoglobin_g_dl: 12.5 });
    render(<PatientFieldEditor store={store} name="hemoglobin_g_dl"
      label="Hemoglobin (g/dL)" labelIncludesUnit />);

    expect(screen.getByText('Hemoglobin (g/dL)')).toBeVisible();
    expect(screen.queryByText('g/dL')).not.toBeInTheDocument();
  });

  it('keeps the descriptor unit when the caller label does not carry it', async () => {
    const { store } = await setup(unitRow(), { hemoglobin_g_dl: 12.5 });
    render(<PatientFieldEditor store={store} name="hemoglobin_g_dl" label="Hemoglobin" />);

    expect(screen.getByText('Hemoglobin')).toBeVisible();
    expect(screen.getByText('g/dL')).toBeVisible();
  });

  it('uses the projection unit as the single fallback', async () => {
    const fields = {
      hemoglobin_g_dl: {
        writable: true, target: 'patient_record', control: 'number',
        label: 'Hemoglobin', projection: { omop_table: 'measurement', unit: 'g/dL' },
      },
    };
    const { store } = await setup(fields, { hemoglobin_g_dl: 12.5 });

    const { unmount } = render(<PatientFieldEditor store={store} name="hemoglobin_g_dl"
      label="Hemoglobin (g/dL)" labelIncludesUnit />);
    expect(screen.queryByText('g/dL')).not.toBeInTheDocument();
    unmount();

    render(<PatientFieldEditor store={store} name="hemoglobin_g_dl" label="Hemoglobin" />);
    expect(screen.getByText('g/dL')).toBeVisible();
  });

  it('suppresses a suffix that differs only in notation', async () => {
    const { store } = await setup({
      wbc_count_thousand_per_ul: {
        writable: true, target: 'patient_record', control: 'number',
        label: 'WBC Count', unit: '10*3/uL',
      },
    }, { wbc_count_thousand_per_ul: 7.5 });
    render(<PatientFieldEditor store={store} name="wbc_count_thousand_per_ul"
      label="WBC Count (10³/µL)" labelIncludesUnit />);

    expect(screen.getByText(/WBC Count \(10³\/µL\)/)).toBeVisible();
    expect(screen.queryByText('10*3/uL')).not.toBeInTheDocument();
  });

  it('does not render a second, dimensionless unit for INR', async () => {
    const { store } = await setup({
      inr: { writable: true, target: 'patient_record', control: 'number',
        label: 'INR', unit: '{INR}' },
    }, { inr: 1.1 });
    render(<PatientFieldEditor store={store} name="inr" label="INR" labelIncludesUnit />);

    expect(screen.getByText('INR')).toBeVisible();
    expect(screen.queryByText('{INR}')).not.toBeInTheDocument();
  });

  it('applies the same rule to a read-only row', async () => {
    const { store } = await setup({
      hemoglobin_g_dl: { writable: false, label: 'Hemoglobin', unit: 'g/dL',
        reason: 'Read-only.' },
    }, { hemoglobin_g_dl: 12.5 });
    render(<PatientFieldEditor store={store} name="hemoglobin_g_dl"
      label="Hemoglobin (g/dL)" labelIncludesUnit />);

    expect(screen.getByText(/Hemoglobin \(g\/dL\)/)).toBeVisible();
    expect(screen.queryByText('g/dL')).not.toBeInTheDocument();
  });

  it('keeps the unit picker when the row owns a source pair', async () => {
    const pairFields = {
      hemoglobin_level: {
        writable: true, target: 'patient_record', control: 'number',
        label: 'Hemoglobin', unit_options: [{ value: 'G/DL', label: 'g/dL' },
          { value: 'G/L', label: 'g/L' }],
      },
      hemoglobin_level_units: {
        writable: false, kind: 'selectable', qualifies: 'hemoglobin_level',
        label: 'Hemoglobin unit',
        unit_options: [{ value: 'G/DL', label: 'g/dL' }, { value: 'G/L', label: 'g/L' }],
      },
    };
    const { store } = await setup(pairFields,
      { hemoglobin_level: 12, hemoglobin_level_units: 'G/DL' });
    render(<PatientFieldEditor store={store} name="hemoglobin_level"
      label="Hemoglobin (g/dL)" labelIncludesUnit />);

    expect(screen.getByRole('combobox', { name: 'Unit for Hemoglobin (g/dL)' }))
      .toHaveValue('G/DL');
  });
});

describe('source-pair unit picker (#5700)', () => {
  const pairFields = {
    hemoglobin_level: {
      writable: true, target: 'patient_record', control: 'number',
      label: 'Hemoglobin', value_kind: 'number',
      unit_options: [{ value: 'G/DL', label: 'g/dL' }, { value: 'G/L', label: 'g/L' }],
    },
    hemoglobin_level_units: {
      writable: false, kind: 'selectable', qualifies: 'hemoglobin_level',
      label: 'Hemoglobin unit',
      unit_options: [{ value: 'G/DL', label: 'g/dL' }, { value: 'G/L', label: 'g/L' }],
    },
  };

  it('preselects the stored unit and sends the value with the chosen unit', async () => {
    const user = userEvent.setup();
    const { store, patch } = await setup(pairFields,
      { hemoglobin_level: 12, hemoglobin_level_units: 'G/DL' });
    render(<PatientFieldEditor store={store} name="hemoglobin_level" label="Hemoglobin" />);

    expect(screen.getByRole('combobox', { name: 'Unit for Hemoglobin' })).toHaveValue('G/DL');
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Hemoglobin' }), { target: { value: '120' } });
    await user.selectOptions(screen.getByRole('combobox', { name: 'Unit for Hemoglobin' }), 'G/L');
    await act(async () => { await store.flush(); });

    expect(patch.mock.calls.at(-1)?.[1]).toEqual({
      hemoglobin_level: 120, hemoglobin_level_units: 'G/L',
    });
  });

  it('treats a unit-only change as a real edit of the same pair', async () => {
    const user = userEvent.setup();
    const { store, patch } = await setup(pairFields,
      { hemoglobin_level: 120, hemoglobin_level_units: 'G/DL' });
    render(<PatientFieldEditor store={store} name="hemoglobin_level" label="Hemoglobin" />);

    await user.selectOptions(screen.getByRole('combobox', { name: 'Unit for Hemoglobin' }), 'G/L');
    await act(async () => { await store.flush(); });

    expect(patch.mock.calls.at(-1)?.[1]).toEqual({
      hemoglobin_level: 120, hemoglobin_level_units: 'G/L',
    });
  });

  it('returns the selector to the saved unit after a refusal', async () => {
    const user = userEvent.setup();
    const { store, patch } = await setup(pairFields,
      { hemoglobin_level: 12, hemoglobin_level_units: 'G/DL' });
    patch.mockRejectedValueOnce({ response: { data: { rejected: {
      hemoglobin_level: 'Hemoglobin is outside the storable range.',
      hemoglobin_level_units: 'Unsupported unit.',
    } } } });
    render(<PatientFieldEditor store={store} name="hemoglobin_level" label="Hemoglobin" />);

    await user.selectOptions(screen.getByRole('combobox', { name: 'Unit for Hemoglobin' }), 'G/L');
    await act(async () => { await store.flush(); });

    expect(screen.getByRole('combobox', { name: 'Unit for Hemoglobin' })).toHaveValue('G/DL');
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Hemoglobin' }), { target: { value: '13' } });
    await act(async () => { await store.flush(); });
    expect(patch.mock.calls.at(-1)?.[1]).toEqual({
      hemoglobin_level: 13, hemoglobin_level_units: 'G/DL',
    });
  });

  it('drops an empty-value draft when another editor supplies the pair', async () => {
    const user = userEvent.setup();
    const { store, patch } = await setup(pairFields,
      { hemoglobin_level: null, hemoglobin_level_units: 'G/DL' });
    render(<>
      <PatientFieldEditor store={store} name="hemoglobin_level" label="Hemoglobin" />
      <PatientFieldEditor store={store} name="hemoglobin_level" label="Hemoglobin again" />
    </>);

    // A draft chosen while there is no value yet.
    await user.selectOptions(screen.getByRole('combobox', { name: 'Unit for Hemoglobin' }), 'G/L');
    expect(screen.getByRole('combobox', { name: 'Unit for Hemoglobin' })).toHaveValue('G/L');

    // Another editor (or a refresh) supplies the authoritative pair.
    act(() => store.applyServerValues({
      hemoglobin_level: 12, hemoglobin_level_units: 'G/DL',
    }));

    expect(screen.getByRole('combobox', { name: 'Unit for Hemoglobin' })).toHaveValue('G/DL');
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Hemoglobin' }), { target: { value: '13' } });
    await act(async () => { await store.flush(); });
    expect(patch.mock.calls.at(-1)?.[1]).toEqual({
      hemoglobin_level: 13, hemoglobin_level_units: 'G/DL',
    });
  });

  it('drops an empty-value draft when a rejected clear restores the pair', async () => {
    const user = userEvent.setup();
    const { store, patch } = await setup(pairFields,
      { hemoglobin_level: 12, hemoglobin_level_units: 'G/DL' });
    let reject!: (reason?: unknown) => void;
    patch.mockImplementationOnce(() => new Promise((_resolve, rejectPromise) => {
      reject = rejectPromise;
    }));
    render(<PatientFieldEditor store={store} name="hemoglobin_level" label="Hemoglobin" />);

    // Clear the value; while the clear is in flight, choose a unit.
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Hemoglobin' }), { target: { value: '' } });
    const pendingClear = store.flush();
    await user.selectOptions(screen.getByRole('combobox', { name: 'Unit for Hemoglobin' }), 'G/L');
    expect(screen.getByRole('combobox', { name: 'Unit for Hemoglobin' })).toHaveValue('G/L');

    // The clear is refused and the saved pair comes back; the draft must not.
    reject({ response: { data: { rejected: {
      hemoglobin_level: 'No.', hemoglobin_level_units: 'No.',
    } } } });
    await act(async () => { await pendingClear; });

    expect(screen.getByRole('combobox', { name: 'Unit for Hemoglobin' })).toHaveValue('G/DL');
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Hemoglobin' }), { target: { value: '13' } });
    await act(async () => { await store.flush(); });
    expect(patch.mock.calls.at(-1)?.[1]).toEqual({
      hemoglobin_level: 13, hemoglobin_level_units: 'G/DL',
    });
  });

  it('does not resurrect an empty-value draft after an external pair comes and goes', async () => {
    const user = userEvent.setup();
    const { store, patch } = await setup(pairFields,
      { hemoglobin_level: null, hemoglobin_level_units: 'G/DL' });
    render(<PatientFieldEditor store={store} name="hemoglobin_level" label="Hemoglobin" />);

    await user.selectOptions(screen.getByRole('combobox', { name: 'Unit for Hemoglobin' }), 'G/L');
    // Another editor writes a pair, then clears it again. The obsolete draft
    // must not come back when the value is empty once more.
    act(() => store.applyServerValues({
      hemoglobin_level: 12, hemoglobin_level_units: 'G/DL',
    }));
    act(() => store.applyServerValues({
      hemoglobin_level: null, hemoglobin_level_units: 'G/DL',
    }));

    expect(screen.getByRole('combobox', { name: 'Unit for Hemoglobin' })).toHaveValue('G/DL');
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Hemoglobin' }), { target: { value: '13' } });
    await act(async () => { await store.flush(); });
    expect(patch.mock.calls.at(-1)?.[1]).toEqual({
      hemoglobin_level: 13, hemoglobin_level_units: 'G/DL',
    });
  });
});

describe('dated source pairs', () => {
  const pairFields = {
    serum_bilirubin_level_direct: {
      label: 'Direct bilirubin', writable: true, target: 'patient_record',
      control: 'number', dated: true, latest_date: '2026-08-01',
      projection: { omop_table: 'measurement' },
    },
    serum_bilirubin_level_direct_units: {
      label: 'Direct bilirubin units', kind: 'selectable', writable: false,
      qualifies: 'serum_bilirubin_level_direct',
      unit_options: [
        { value: 'MG/DL', label: 'mg/dL' },
        { value: 'UMOL/L', label: 'umol/L' },
      ],
    },
  };
  const pairValues = {
    serum_bilirubin_level_direct: 1.2,
    serum_bilirubin_level_direct_units: 'MG/DL',
  };

  it('saves a dated value, its date and its unit together', async () => {
    const { store, patch } = await setup(pairFields, pairValues);
    render(<PatientFieldEditor store={store} name="serum_bilirubin_level_direct" />);

    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '10' } });
    fireEvent.change(screen.getByLabelText('Unit for Direct bilirubin'),
      { target: { value: 'UMOL/L' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save result' }));
    await act(async () => { await store.flush(); });

    expect(patch.mock.calls[0][1]).toEqual({
      serum_bilirubin_level_direct: { value: 10, date: '2026-08-01' },
      serum_bilirubin_level_direct_units: 'UMOL/L',
    });
  });

  it('does not save a dated unit change before Save result', async () => {
    const { store, patch } = await setup(pairFields, pairValues);
    render(<PatientFieldEditor store={store} name="serum_bilirubin_level_direct" />);

    fireEvent.change(screen.getByLabelText('Unit for Direct bilirubin'),
      { target: { value: 'UMOL/L' } });
    await act(async () => { await store.flush(); });
    expect(patch).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Save result' }));
    await act(async () => { await store.flush(); });

    expect(patch.mock.calls[0][1]).toEqual({
      serum_bilirubin_level_direct: { value: 1.2, date: '2026-08-01' },
      serum_bilirubin_level_direct_units: 'UMOL/L',
    });
  });
});
