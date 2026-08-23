/**
 * The fields step 5 surfaced: from the UI, into OMOP or Person, and back.
 *
 * All of these were writable and shown by no tab, so nothing had ever exercised
 * their write path. A descriptor entry is a claim; this is the check.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, beforeAll, vi } from 'vitest';
import DiseaseTab from './DiseaseTab';
import GeneralTab from './GeneralTab';
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

describe('step 5 fields round trip (live server)', () => {
  it('a measurement surfaced on DiseaseTab writes and derives back', async () => {
    const d = await fetchWritableFields();
    const before = await record();
    const next = Number(before.pd_l1_combined_positive_score) === 15 ? 25 : 15;

    await writeFieldValue(PERSON, 'pd_l1_combined_positive_score',
      d.pd_l1_combined_positive_score, next);

    const after = await record();
    expect(Number(after.pd_l1_combined_positive_score)).toBe(next);

    render(<DiseaseTab formData={after} onChange={vi.fn()} onMutationAdd={vi.fn()}
      onMutationRemove={vi.fn()} onMutationChange={vi.fn()} diseaseType="breast" />);
    await waitFor(() =>
      expect(screen.getByDisplayValue(String(next))).toBeInTheDocument(),
    );
  });

  it('a Person field surfaced on GeneralTab writes and derives back', async () => {
    const d = await fetchWritableFields();
    const before = await record();
    const next = before.facility_name === 'Dana-Farber' ? 'Mass General' : 'Dana-Farber';

    await writeFieldValue(PERSON, 'facility_name', d.facility_name, next);

    const after = await record();
    expect(after.facility_name).toBe(next);

    render(<GeneralTab formData={after} onChange={vi.fn()} editedName="T"
      onNameChange={vi.fn()} onZipcodeChange={vi.fn()} diseaseType="breast" />);
    await waitFor(() =>
      expect(screen.getByDisplayValue(next)).toBeInTheDocument(),
    );
  });

  it('the clinician validation flags write to Person', async () => {
    const d = await fetchWritableFields();
    const before = await record();
    const next = before.validated_by === 'Dr Chen' ? 'Dr Okafor' : 'Dr Chen';

    await writeFieldValue(PERSON, 'validated_by', d.validated_by, next);

    expect((await record()).validated_by).toBe(next);
  });
});
