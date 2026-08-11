/**
 * Tests for GeneralTab age display (issue #456).
 *
 * OMOP-derived PatientRecords only carry `date_of_birth` when the source
 * Person has full month/day precision. Without a fallback the read-only Age
 * box renders blank for every ETL-loaded patient even though the API returns
 * a derived `age`.
 */

import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest';
import GeneralTab from './GeneralTab';
import { useVocabulary } from '@/hooks/useVocabulary';

vi.mock('@/hooks/useVocabulary', () => ({ useVocabulary: vi.fn() }));

vi.mock('@/components/UI/VocabularyTooltip', () => ({
  VocabularyTooltip: () => null,
}));

const baseProps = {
  onChange: vi.fn(),
  editedName: 'Test Patient',
  onNameChange: vi.fn(),
  onZipcodeChange: vi.fn(),
};

function ageInput(): HTMLInputElement {
  const label = screen.getByText('Age');
  const container = label.parentElement as HTMLElement;
  return container.querySelector('input') as HTMLInputElement;
}

describe('GeneralTab — Age display', () => {
  beforeEach(() => {
    (useVocabulary as Mock).mockReturnValue({ options: [], source: 'fallback' });
  });

  it('falls back to the API-derived age when date_of_birth is null', () => {
    render(<GeneralTab {...baseProps} formData={{ date_of_birth: null, age: 70 }} />);
    expect(ageInput().value).toBe('70');
  });

  it('falls back to patient_age when neither date_of_birth nor age is present', () => {
    render(<GeneralTab {...baseProps} formData={{ patient_age: 55 }} />);
    expect(ageInput().value).toBe('55');
  });

  it('prefers date_of_birth over the derived age', () => {
    const dob = new Date();
    dob.setFullYear(dob.getFullYear() - 42);
    render(
      <GeneralTab
        {...baseProps}
        formData={{ date_of_birth: dob.toISOString().slice(0, 10), age: 999 }}
      />,
    );
    expect(ageInput().value).toBe('42');
  });

  it('renders empty when no age information exists at all', () => {
    render(<GeneralTab {...baseProps} formData={{}} />);
    expect(ageInput().value).toBe('');
  });
});
