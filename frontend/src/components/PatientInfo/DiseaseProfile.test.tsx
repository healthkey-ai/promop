import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import ClinicalField from './ClinicalField';
import { DiseaseProfileContext } from './DiseaseProfile';
import { isMemberOf } from '@/utils/fieldScope';

const writable = { kind: 'direct' as const, writable: true, target: 'patient_record' };

const field = (diseases: string[] | undefined, value: unknown) => (
  <ClinicalField
    label="Bulky Disease" name="bulky_disease" type="text" value={value}
    descriptor={{ ...writable, diseases }} onChange={vi.fn()}
  />
);

const onTab = (category: string, node: React.ReactNode) => (
  <DiseaseProfileContext.Provider value={{ category, label: 'Follicular Lymphoma' }}>{node}</DiseaseProfileContext.Provider>
);

describe('isMemberOf (#2332)', () => {
  it('is true for the base record and for a named category only', () => {
    expect(isMemberOf(undefined, 'MM')).toBe(true);
    expect(isMemberOf({ diseases: [] }, 'MM')).toBe(true);
    expect(isMemberOf({ diseases: ['FL', 'MCL'] }, 'MCL')).toBe(true);
    expect(isMemberOf({ diseases: ['FL'] }, 'MM')).toBe(false);
    expect(isMemberOf({ diseases: ['FL'] }, '')).toBe(false);
  });
});

describe('ClinicalField on the disease tab (#2332)', () => {
  it('renders a member field as it always did', () => {
    render(onTab('FL', field(['FL'], 'yes')));
    expect(screen.getByRole('textbox')).not.toBeDisabled();
    expect(screen.queryByTestId('reason-bulky_disease')).toBeNull();
  });

  it('renders a base field on any category', () => {
    render(onTab('MM', field(undefined, 'yes')));
    expect(screen.getByRole('textbox')).not.toBeDisabled();
  });

  it('shows a non-member field that holds a value read-only, marked, and never clears it', () => {
    render(onTab('MM', field(['FL'], 'yes')));
    expect(screen.getByRole('textbox')).toHaveValue('yes');
    expect(screen.getByRole('textbox')).toBeDisabled();
    expect(screen.getByTestId('reason-bulky_disease')).toHaveTextContent('Not part of the Follicular Lymphoma record.');
  });

  it('shows nothing for a non-member field with no value', () => {
    const { container } = render(onTab('MM', field(['FL'], '')));
    expect(container).toBeEmptyDOMElement();
  });

  it('holds no field to a disease outside the disease tab', () => {
    render(field(['FL'], ''));
    expect(screen.getByRole('textbox')).not.toBeDisabled();
  });
});
