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
import type { FieldDescriptor } from '@/hooks/useWritableFields';

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
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

describe('ClinicalField writability', () => {
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
