import { describe, expect, it, vi } from 'vitest';
import api from '@/api/axios';
import { fetchRegimens } from './mappingHub';

vi.mock('@/api/axios', () => ({ default: { get: vi.fn() } }));

describe('regimen curation pagination', () => {
  it('includes a regimen after the first 50 without changing the search', async () => {
    vi.mocked(api.get)
      .mockResolvedValueOnce({ data: Array.from({ length: 50 }, (_, n) => ({ code: `r${n}`, title: `Regimen ${n}` })) })
      .mockResolvedValueOnce({ data: [{ code: 'last', title: 'Last regimen' }] });
    const rows = await fetchRegimens('regimen');
    expect(rows).toHaveLength(51);
    expect(rows[50].code).toBe('last');
    expect(api.get).toHaveBeenLastCalledWith('/v1/therapy-regimens/', { params: { search: 'regimen', offset: 50 } });
  });
});
