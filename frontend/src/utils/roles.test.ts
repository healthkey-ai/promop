import { describe, expect, it } from 'vitest';

import type { User } from '@/hooks/useAuth';

import { canOpenPatients, canReadPatients, isAnalystOnly } from './roles';

const grant = (role: string, extra: Record<string, unknown> = {}) => ({
  org_name: 'Org', org_slug: 'org', role, expires_at: null, ...extra,
});

const user = (accesses: ReturnType<typeof grant>[]): User =>
  ({ id: 1, email: 'u@test.com', org_accesses: accesses }) as unknown as User;

describe('canReadPatients / isAnalystOnly', () => {
  it('routes an analyst at a private org away from the patient list', () => {
    const u = user([grant('analyst')]);
    expect(canReadPatients(u)).toBe(false);
    expect(isAnalystOnly(u)).toBe(true);
  });

  it('opens the patient list to an analyst at a public demo org (#1392, #2416)', () => {
    const u = user([grant('analyst', { org_allows_patient_signup: true })]);
    expect(canReadPatients(u)).toBe(true);
    expect(isAnalystOnly(u)).toBe(false);
    // The list only: no detail page, no names (#2460).
    expect(canOpenPatients(u)).toBe(false);
    expect(canOpenPatients(user([grant('doctor', { org_allows_patient_signup: true })]))).toBe(true);
  });

  it('does not count a patient-role grant at a demo org as a professional read', () => {
    const u = user([grant('patient', { org_allows_patient_signup: true })]);
    expect(canReadPatients(u)).toBe(false);
  });
});
