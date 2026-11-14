/**
 * Rendering a field according to what the server says can be done with it.
 *
 * The projection owns no writable clinical column, so a box is typeable only
 * when the descriptor names the write behind it. Everything else renders
 * read-only *with its reason*: a computed field, or one that mirrors another
 * column, is not broken, and saying so is the difference between a UI that looks
 * unfinished and one that explains itself.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi, describe, it, expect } from 'vitest';
import ClinicalField from './ClinicalField';
import { PatientFormContext } from '@/federation/PatientFormContext';
import type { FieldDescriptor } from '@/hooks/useWritableFields';

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
}));

const federatedEditorProps = vi.hoisted(() => vi.fn());
vi.mock('@/federation/PatientFieldEditor', () => ({
  default: (props: Record<string, unknown>) => {
    federatedEditorProps(props);
    return <div data-testid="federated-editor" />;
  },
}));

const CURATED = {
  kind: 'profile', writable: true, target: 'person', payload_field: 'gender',
  value_kind: 'string',
  options: [{ value: 'Female', code: 'F' }, { value: 'Male', code: 'M' }],
} as unknown as FieldDescriptor;

async function openSelect() {
  const trigger = screen.getByRole('combobox');
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: 'mouse' });
  fireEvent.click(trigger);
  await waitFor(() => expect(screen.getByRole('listbox')).toBeInTheDocument());
  return Array.from(screen.getByRole('listbox').querySelectorAll('[role="option"]'))
    .map((o) => o.textContent?.trim())
    .filter((t) => t && !t.startsWith('—'));
}

describe('ClinicalField choices', () => {
  it('prefers the curated options the descriptor carries', async () => {
    // The descriptor's set is what the server resolves a concept from. A local
    // list is a display convenience, and offering a value from it that the
    // server cannot code would produce a write that stores text and no concept.
    render(
      <ClinicalField
        label="Gender" name="gender" type="select" value="Female"
        descriptor={CURATED}
        options={['Female', 'Male', 'Other', 'Prefer not to say']}
        onChange={vi.fn()}
      />,
    );

    expect(await openSelect()).toEqual(['Female', 'Male']);
  });

  it('falls back to the local list when the descriptor carries none', async () => {
    // Country is writable on Person but has no curated set, so the tab's own
    // list is the only source.
    const country = {
      kind: 'profile', writable: true, target: 'person',
      payload_field: 'country', value_kind: 'string',
    } as unknown as FieldDescriptor;
    render(
      <ClinicalField
        label="Country" name="country" type="select" value="United States"
        descriptor={country} options={['United States', 'Canada']}
        onChange={vi.fn()}
      />,
    );

    expect(await openSelect()).toEqual(['United States', 'Canada']);
  });
});

describe('ClinicalField federated editor', () => {
  it('forwards a reviewed label that carries its unit (#5704)', () => {
    render(
      <PatientFormContext.Provider value={{} as never}>
        <ClinicalField
          label="Hemoglobin (g/dL)" labelIncludesUnit
          name="hemoglobin_g_dl" type="number" value={12.5}
          onChange={vi.fn()}
        />
      </PatientFormContext.Provider>,
    );

    expect(screen.getByTestId('federated-editor')).toBeInTheDocument();
    expect(federatedEditorProps).toHaveBeenCalledWith(expect.objectContaining({
      name: 'hemoglobin_g_dl', label: 'Hemoglobin (g/dL)', labelIncludesUnit: true,
    }));
  });

  it('forwards the tab choices for a descriptor without options', () => {
    render(
      <PatientFormContext.Provider value={{} as never}>
        <ClinicalField label="Post-Transformation Outcome" name="post_transformation_outcome"
          type="select" value="" options={['Complete response', 'Progression']} onChange={vi.fn()} />
      </PatientFormContext.Provider>,
    );

    expect(federatedEditorProps).toHaveBeenCalledWith(expect.objectContaining({
      name: 'post_transformation_outcome', options: ['Complete response', 'Progression'],
    }));
  });
});

describe('ClinicalField writability', () => {
  it('sends a dated value without a date until one is picked (#2454)', () => {
    const onChange = vi.fn();
    render(
      <ClinicalField
        label="Ki-67" name="ki67_proliferation_index" type="number"
        value={20}
        descriptor={{
          kind: 'direct', writable: true, target: 'patient_record',
          value_kind: 'number', dated: true,
        } as FieldDescriptor}
        onChange={onChange}
      />,
    );

    fireEvent.change(screen.getByDisplayValue('20'), { target: { value: '25' } });
    expect(onChange).toHaveBeenCalledWith('ki67_proliferation_index', 25);

    fireEvent.change(document.getElementById('ki67_proliferation_index-date')!,
      { target: { value: '2026-10-01' } });
    expect(onChange).toHaveBeenLastCalledWith(
      'ki67_proliferation_index', 20, '2026-10-01');
  });

  it('edits a normalised canonical lab without the form store (#2425)', () => {
    const canonical = {
      kind: 'computed', writable: false, target: 'patient_record', inputs: ['hemoglobin_level'],
      normalised_read_model: true, reason: 'Normalised from hemoglobin_level.',
    } as unknown as FieldDescriptor;
    render(
      <ClinicalField label="Hemoglobin (g/dL)" name="hemoglobin_g_dl" type="number" value={12}
        descriptor={canonical} onChange={vi.fn()} />,
    );

    expect(screen.getByDisplayValue('12')).not.toBeDisabled();
    expect(screen.queryByTestId('reason-hemoglobin_g_dl')).not.toBeInTheDocument();
  });

  it('keeps a normalised canonical read-only for a caller without write access', () => {
    const canonical = {
      kind: 'computed', writable: false, target: 'patient_record', normalised_read_model: true,
      read_only_for_caller: true, reason: 'You have read-only access to this patient record.',
    } as unknown as FieldDescriptor;
    render(
      <ClinicalField label="Hemoglobin (g/dL)" name="hemoglobin_g_dl" type="number" value={12}
        descriptor={canonical} onChange={vi.fn()} />,
    );

    expect(screen.getByDisplayValue('12')).toBeDisabled();
  });

  it('renders read-only with the reason when the server refuses the field', () => {
    const computed = {
      kind: 'computed', writable: false, inputs: ['height', 'weight'],
      reason: 'Computed from height, weight.',
    } as unknown as FieldDescriptor;
    render(
      <ClinicalField label="BMI" name="bmi" type="number" value={24.2}
        descriptor={computed} onChange={vi.fn()} />,
    );

    expect(screen.getByTestId('reason-bmi')).toHaveTextContent(/computed from/i);
    expect(screen.getByDisplayValue('24.2')).toBeDisabled();
  });

  it('treats an absent descriptor as read-only', () => {
    // Failing closed: offering an edit the server will refuse is worse than
    // showing a value that cannot yet change.
    render(
      <ClinicalField label="Mystery" name="mystery" type="text" value="x"
        onChange={vi.fn()} />,
    );

    expect(screen.getByTestId('reason-mystery')).toHaveTextContent(/not editable here/i);
  });

  it('can suppress the reason for a tab where every field shares one', () => {
    // Printing the same paragraph beside 25 boxes buries it; the treatment tab
    // states it once instead.
    render(
      <ClinicalField label="BMI" name="bmi" type="number" value={24.2}
        descriptor={{ kind: 'computed', writable: false, reason: 'Computed.' } as unknown as FieldDescriptor}
        onChange={vi.fn()} showReason={false} />,
    );

    expect(screen.queryByTestId('reason-bmi')).not.toBeInTheDocument();
    expect(screen.getByDisplayValue('24.2')).toBeDisabled();
  });
});
