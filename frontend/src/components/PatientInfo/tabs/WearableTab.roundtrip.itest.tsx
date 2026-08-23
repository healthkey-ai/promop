/**
 * Wearable tab against the real descriptor.
 *
 * Nothing here is writable, so there is no write to round-trip. What can go
 * wrong instead is the tab claiming otherwise, or the twenty fields the server
 * describes drifting from the twenty the tab renders — neither of which a mocked
 * descriptor would catch, because the mock is written from the same assumption
 * as the component.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, beforeAll, vi } from 'vitest';
import WearableTab from './WearableTab';
import { fetchWritableFields, __resetWritableFieldsCache } from '@/hooks/useWritableFields';
import api from '@/api/axios';

const PERSON = 262;

beforeAll(() => {
  document.cookie = 'sessionid=svlpo0jc6pm4ey52ih43ruykpv2cio51';
  document.cookie = 'csrftoken=aTaONYK8cVsQ9ZR0dOt8QlfacFA4iomG';
  __resetWritableFieldsCache();
});

describe('WearableTab against the live descriptor', () => {
  it('the server agrees that no wearable field is writable', async () => {
    const d = await fetchWritableFields();
    const wearable = Object.entries(d).filter(([f]) => f.endsWith('_30d'));

    expect(wearable.length).toBeGreaterThan(0);
    // The invariant is that none is writable. They are not all the same kind:
    // the clinical aggregates are `computed`, while wearable_coverage_ratio_30d
    // is device provenance rather than a measurement of the patient, so it sits
    // in the wearable-metadata group instead. Asserting one kind for all of them
    // would fail on a correct distinction.
    for (const [field, entry] of wearable) {
      expect(entry.writable, `${field} is offered as writable`).toBe(false);
    }
    const kinds = new Set(wearable.map(([, e]) => e.kind));
    expect(kinds.has('computed')).toBe(true);
  });

  it('renders every value read-only, with the upload control that does change them', async () => {
    await fetchWritableFields();
    const record = (await api.get(`/patient-info/${PERSON}/`)).data.patient_info;
    render(<WearableTab formData={record} onChange={vi.fn()} />);

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /upload/i })).toBeInTheDocument(),
    );
    const inputs = [...screen.queryAllByRole('textbox'),
                    ...screen.queryAllByRole('spinbutton')];
    expect(inputs.length).toBeGreaterThan(0);
    for (const input of inputs) expect(input).toBeDisabled();
  });
});
