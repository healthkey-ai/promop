import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.fn();
const client = { get };
vi.mock('@/api/clinicalTransport', () => ({
  clinicalClient: () => client,
  clinicalUrl: (path: string) => `/api/v1/federation${path}`,
}));

import { useVocabulary } from './useVocabulary';

beforeEach(() => { get.mockReset(); });

describe('useVocabulary transport', () => {
  it('asks the host API prefix instead of the static origin root', async () => {
    // The federated host serves its whole origin as a static SPA; a
    // root-relative /vocabularies/ request returns index.html, not JSON, and
    // the old hook swallowed the parse failure into an empty picker (#5599).
    get.mockResolvedValue({ data: [{ code: 'infections', title: 'Infections' }] });
    const { result } = renderHook(() =>
      useVocabulary('pre-existing-condition-category', 'code'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(get).toHaveBeenCalledWith(
      '/api/v1/federation/vocabularies/pre-existing-condition-category/');
    expect(result.current.options).toEqual([{ value: 'infections', label: 'Infections' }]);
  });
});
