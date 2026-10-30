/**
 * The explicit pre-existing-conditions editor is not an ordinary record field.
 *
 * Its snapshot and write live on a dedicated endpoint, and the federated host
 * deliberately keeps both columns out of the generic descriptor — a PATCH to
 * them is not how they are authored. The tab must therefore gate it on the
 * host's write permission, not on a descriptor entry that is absent by design
 * (#5599).
 */
import { act, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import GeneralTab from './GeneralTab';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';

const mockGet = vi.fn();
vi.mock('@/api/axios', () => ({
  default: { get: (...args: unknown[]) => mockGet(...args), post: vi.fn(), patch: vi.fn() },
}));

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({
    options: [{ value: 'infections', label: 'Infections' }],
    source: null,
    loading: false,
  }),
}));

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  // No descriptor entry for preexisting_conditions, exactly as the federated
  // host answers: the generic surface excludes the explicit-answer fields.
  mockGet.mockResolvedValue({ data: {} });
});

describe('GeneralTab pre-existing conditions', () => {
  it('keeps the explicit answer editor usable without a descriptor entry', async () => {
    await act(async () => {
      render(
        <GeneralTab
          formData={{
            person_id: 42,
            no_pre_existing_conditions: true,
            preexisting_conditions: [],
          }}
          onChange={vi.fn()}
          editedName="Alishia Howell"
          onNameChange={vi.fn()}
          onZipcodeChange={vi.fn()}
          onConditionsSaved={vi.fn()}
        />,
      );
    });

    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(await screen.findByLabelText('Infections')).toBeEnabled();
    expect(screen.getByRole('button', { name: /Save categories/ })).toBeEnabled();
  });
});
