import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.fn();
const client = { get };
vi.mock('@/api/clinicalTransport', () => ({
  clinicalClient: () => client,
  clinicalUrl: (path: string) => `/api/v1/federation${path}`,
}));

import {
  __resetDiseaseCategoryCaches,
  useAllDiseaseMappings,
  useCategoryObjects,
  useDiseaseCategories,
  useDiseasesByCategory,
} from './useDiseaseCategories';

beforeEach(() => {
  get.mockReset();
  __resetDiseaseCategoryCaches();
});

describe('disease category hooks use the clinical transport', () => {
  it('asks the host API prefix for the category list', async () => {
    // The federated host serves its whole origin as a static SPA; a
    // root-relative request returns index.html with a 200 (#5599, #5847).
    get.mockResolvedValue({ data: [
      { value: 'MM', label: 'Multiple Myeloma' },
      { value: 'BC', label: 'Breast Cancer' },
    ] });
    const { result } = renderHook(() => useDiseaseCategories());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(get).toHaveBeenCalledWith('/api/v1/federation/v1/disease-categories/');
    expect(result.current.options).toEqual(['Multiple Myeloma', 'Breast Cancer']);
  });

  it('keeps the category objects the dropdown sends as filter values', async () => {
    get.mockResolvedValue({ data: [{ value: 'MM', label: 'Multiple Myeloma' }] });
    const { result } = renderHook(() => useCategoryObjects());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(get).toHaveBeenCalledWith('/api/v1/federation/v1/disease-categories/');
    expect(result.current.categories).toEqual([{ value: 'MM', label: 'Multiple Myeloma' }]);
  });

  it('asks the host API prefix for one category and passes the filter', async () => {
    get.mockResolvedValue({ data: [
      { disease_name: 'Multiple Myeloma', category: 'MM' },
    ] });
    const { result } = renderHook(() => useDiseasesByCategory('MM'));
    // `loading` starts false here, so waiting on it would resolve before the
    // mocked response lands; wait on the value the hook exists to produce.
    await waitFor(() =>
      expect(result.current.diseases).toEqual(['Multiple Myeloma']));

    expect(get).toHaveBeenCalledWith(
      '/api/v1/federation/v1/disease-category-mappings/',
      { params: { category: 'MM' } },
    );
  });

  it('maps disease names to categories through the host prefix', async () => {
    get.mockResolvedValue({ data: [
      { disease_name: 'Multiple Myeloma', category: 'MM' },
      { disease_name: 'Breast Cancer', category: 'BC' },
    ] });
    const { result } = renderHook(() => useAllDiseaseMappings());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(get).toHaveBeenCalledWith('/api/v1/federation/v1/disease-category-mappings/');
    expect(result.current.categoryForDisease('multiple myeloma')).toBe('MM');
    expect(result.current.diseasesByCategory).toEqual({
      MM: ['Multiple Myeloma'],
      BC: ['Breast Cancer'],
    });
  });

  it('renders an empty grouping instead of throwing on a 200 HTML shell', async () => {
    // A host that does not route the path answers its own SPA shell; axios
    // hands that back as a string and iterating it used to throw during
    // render, unmounting the whole patient widget (#5847).
    get.mockResolvedValue({ data: '<!doctype html><html></html>' });
    const { result } = renderHook(() => useAllDiseaseMappings());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.categoryForDisease('Multiple Myeloma')).toBeNull();
    expect(result.current.diseasesByCategory).toEqual({});
  });

  it('does not cache an unroutable body on the label path either', async () => {
    get.mockResolvedValueOnce({ data: '<!doctype html><html></html>' });
    const first = renderHook(() => useCategoryObjects());
    await waitFor(() => expect(first.result.current.loading).toBe(false));
    expect(first.result.current.categories).toEqual([]);
    first.unmount();

    get.mockResolvedValueOnce({ data: [{ value: 'MM', label: 'Multiple Myeloma' }] });
    const second = renderHook(() => useCategoryObjects());
    await waitFor(() =>
      expect(second.result.current.categories).toEqual([
        { value: 'MM', label: 'Multiple Myeloma' },
      ]));

    expect(get).toHaveBeenCalledTimes(2);
  });

  it('does not cache an unroutable body, so a later mount fetches again', async () => {
    // The 200 HTML shell is a host that does not route the path, not an empty
    // catalog. Caching it as the answer would leave every later mount empty
    // after the host is fixed, until a full page reload.
    get.mockResolvedValueOnce({ data: '<!doctype html><html></html>' });
    const first = renderHook(() => useAllDiseaseMappings());
    await waitFor(() => expect(first.result.current.loading).toBe(false));
    expect(first.result.current.diseasesByCategory).toEqual({});
    first.unmount();

    get.mockResolvedValueOnce({
      data: [{ disease_name: 'Breast Cancer', category: 'BC' }],
    });
    const second = renderHook(() => useAllDiseaseMappings());
    await waitFor(() => expect(second.result.current.loading).toBe(false));

    expect(get).toHaveBeenCalledTimes(2);
    expect(second.result.current.diseasesByCategory).toEqual({ BC: ['Breast Cancer'] });
  });
});
