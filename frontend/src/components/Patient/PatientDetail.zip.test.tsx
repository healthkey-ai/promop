import { MemoryRouter } from 'react-router-dom';
/**
 * ZIP autofill → the PatientRecord PATCH (#1665).
 *
 * `region` is projected to OMOP Location.state, two characters, and the PATCH is refused whole
 * for a longer value. The lookup's `state` is the full name, so the editor must send `state
 * abbreviation` — and nothing when it is missing. The lookup is US-only, so it must not run for
 * a patient whose country is set to something else. Same rules as the widget (#1646).
 */
import { render as baseRender, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import PatientDetail from './PatientDetail';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';

vi.mock('@/api/axios', () => ({
  default: { get: vi.fn(), patch: vi.fn(), post: vi.fn() },
}));

vi.mock('react-router-dom', async importOriginal => ({
  ...await importOriginal<typeof import("react-router-dom")>(),
  useParams: () => ({ personId: '261' }),
  useNavigate: () => vi.fn(),
}));

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
}));

import api from '@/api/axios';

const location = (person_field: string) => ({
  kind: 'direct', writable: true, target: 'patient_record',
  projection_target: 'location', person_field, value_kind: 'string',
});

const DESCRIPTORS = {
  country: location('Location.country'),
  region: location('Location.state'),
  city: location('Location.city'),
  postal_code: location('Location.zip'),
};

const BEVERLY_HILLS = { 'place name': 'Beverly Hills', state: 'California', 'state abbreviation': 'CA' };

let country: string | null;

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
    if (url.includes('writable-fields')) return Promise.resolve({ data: DESCRIPTORS });
    if (url.includes('/patient-info/')) {
      return Promise.resolve({
        data: {
          patient_info: { person_id: 261, patient_name: 'Ada Lovelace', country, region: null, city: null, postal_code: null },
          user: null,
          patient_name: 'Ada Lovelace',
        },
      });
    }
    return Promise.resolve({ data: [] });
  });
  (api.patch as ReturnType<typeof vi.fn>).mockResolvedValue({ data: {} });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function mockLookup(place: Record<string, string>) {
  const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ places: [place] }) }));
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

async function typeZipAndSave(zip: string) {
  baseRender(<MemoryRouter><PatientDetail /></MemoryRouter>);
  await waitFor(() => expect(screen.getByDisplayValue('Ada Lovelace')).toBeInTheDocument());
  vi.useFakeTimers({ shouldAdvanceTime: true });
  fireEvent.change(screen.getByPlaceholderText(/5-digit US zip code/i), { target: { value: zip } });
  // Let the lookup resolve and render before the 2s autosave fires, as it does in a browser.
  // Inside one act() the render is deferred to the end, after the timer.
  await act(async () => {});
  await act(async () => { vi.advanceTimersByTime(2100); });
  await waitFor(() => expect(api.patch).toHaveBeenCalled());
  const calls = (api.patch as ReturnType<typeof vi.fn>).mock.calls;
  return calls.at(-1)![1] as Record<string, unknown>;
}

describe('PatientDetail ZIP autofill', () => {
  it('sends the state abbreviation, not the full name', async () => {
    country = 'United States';
    mockLookup(BEVERLY_HILLS);
    const sent = await typeZipAndSave('90210');
    expect(sent).toStrictEqual({ postal_code: '90210', city: 'Beverly Hills', region: 'CA' });
  });

  it('leaves region out when the lookup has no abbreviation', async () => {
    country = 'United States';
    mockLookup({ 'place name': 'Beverly Hills', state: 'California' });
    const sent = await typeZipAndSave('90210');
    expect(sent).toStrictEqual({ postal_code: '90210', city: 'Beverly Hills' });
  });

  // Unset, and the spellings imports (FHIR/Synthea "US") and CB ("United States of America") write.
  it.each([null, '', 'US', 'united states', 'United States of America'])(
    'looks up when country is %j',
    async (value) => {
      country = value;
      mockLookup(BEVERLY_HILLS);
      const sent = await typeZipAndSave('90210');
      expect(sent).toMatchObject({ postal_code: '90210', city: 'Beverly Hills', region: 'CA' });
    },
  );

  it('does not look up a 5-digit postcode outside the US', async () => {
    country = 'Germany';
    const fetch = mockLookup(BEVERLY_HILLS);
    const sent = await typeZipAndSave('10115');
    expect(sent).toStrictEqual({ postal_code: '10115' });
    expect(fetch).not.toHaveBeenCalled();
  });
});
