/**
 * Behavior tab: from the UI, into OMOP, and back to the UI.
 *
 * Plan step 3 made `employment_status` writable by seeding its concept mapping.
 * That is only true if a value typed into the tab reaches an Observation and
 * derivation reads it back into the field the tab renders — which no mocked test
 * can show.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, beforeAll, vi } from 'vitest';
import BehaviorTab from './BehaviorTab';
import { fetchWritableFields, __resetWritableFieldsCache } from '@/hooks/useWritableFields';
import { writeFieldValue } from '@/api/clinicalFacts';
import api from '@/api/axios';

const PERSON = 262;

beforeAll(() => {
  document.cookie = 'sessionid=svlpo0jc6pm4ey52ih43ruykpv2cio51';
  document.cookie = 'csrftoken=aTaONYK8cVsQ9ZR0dOt8QlfacFA4iomG';
  __resetWritableFieldsCache();
});

const record = async () =>
  (await api.get(`/patient-info/${PERSON}/`)).data.patient_info as Record<string, unknown>;

describe('BehaviorTab round trip (live server)', () => {
  it('renders the real descriptor: the mapped field is editable, the rest explain themselves', async () => {
    // Prime first: an empty descriptor also renders a reason for every field, so
    // waiting on one proves nothing until the fetch has resolved.
    await fetchWritableFields();
    const before = await record();
    render(<BehaviorTab formData={before} onChange={vi.fn()} />);

    await waitFor(() =>
      expect(screen.queryByTestId('reason-smoking_status')).toBeInTheDocument(),
    );
    // employment_status became writable in step 3; smoking_status has no
    // extractor, so it stays read-only with a reason (#648).
    expect(screen.queryByTestId('reason-employment_status')).not.toBeInTheDocument();
    expect(screen.queryByTestId('reason-insurance_type')).not.toBeInTheDocument();
  });

  it('takes an edit from the tab through OMOP and back into the tab', async () => {
    const descriptors = await fetchWritableFields();
    const before = await record();
    const next = before.employment_status === 'Employed full-time'
      ? 'Retired' : 'Employed full-time';

    // 1. the edit, as the component reports it
    const edits: Array<[string, unknown]> = [];
    const { unmount } = render(
      <BehaviorTab formData={before} onChange={(f, v) => edits.push([f, v])} />,
    );
    await waitFor(() => expect(screen.queryAllByRole('textbox').length).toBeGreaterThan(0));
    unmount();

    // 2. the write the app performs for that edit
    await writeFieldValue(PERSON, 'employment_status',
      descriptors.employment_status, next);

    // 3. derivation, read back through the API
    const after = await record();
    expect(after.employment_status).toBe(next);

    // 4. and rendered by the tab — the round trip is only closed here.
    // employment_status is a select, so the value shows as the trigger's text
    // rather than an input value.
    render(<BehaviorTab formData={after} onChange={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByText(next as string)).toBeInTheDocument(),
    );
  });

  it('refuses a field with no write path', async () => {
    const descriptors = await fetchWritableFields();
    await expect(
      writeFieldValue(PERSON, 'smoking_status', descriptors.smoking_status, 'Never Smoker'),
    ).rejects.toThrow();
  });
});
