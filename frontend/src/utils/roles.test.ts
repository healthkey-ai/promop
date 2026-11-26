import { describe, expect, it } from 'vitest';

import type { User } from '@/hooks/useAuth';

import { canOpenPatients, canReadPatients, isAnalystOnly, isOwnRecord, ownPersonId, ownRecordPath } from './roles';

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

describe('ownPersonId / isOwnRecord (#2816)', () => {
  const link = (person_id?: number, source = 'patient_link') => ({
    role: 'patient', scope: 'patient', source, person_id, expires_at: null,
  });
  const account = (extra: Record<string, unknown>): User =>
    ({ id: 1, email: 'u@test.com', ...extra }) as unknown as User;

  it("is a patient-only account's person_id", () => {
    const u = account({ is_patient: true, person_id: 7 });
    expect(ownPersonId(u)).toBe(7);
    expect(isOwnRecord(u, '7')).toBe(true);
    expect(isOwnRecord(u, 7)).toBe(true);
    expect(isOwnRecord(u, '70')).toBe(false);
  });

  it('is the patient link of an account that is also staff or a professional', () => {
    const u = account({
      is_staff: true, is_patient: false, person_id: null,
      effective_roles: [{ role: 'staff', scope: 'platform', source: 'staff_flag', expires_at: null }, link(12)],
    });
    expect(ownPersonId(u)).toBe(12);
    expect(isOwnRecord(u, '12')).toBe(true);
    expect(isOwnRecord(u, '13')).toBe(false);
  });

  it('is nobody for an account with no patient link', () => {
    const u = account({ is_staff: true, effective_roles: [{ role: 'org_admin', scope: 'organization', source: 'org_grant', expires_at: null }] });
    expect(ownPersonId(u)).toBeNull();
    expect(isOwnRecord(u, '12')).toBe(false);
  });

  it('does not read a patient-role grant at an organization as a record of its own', () => {
    // The grant names an organization, not a person.
    const u = account({ effective_roles: [link(undefined, 'org_grant')] });
    expect(ownPersonId(u)).toBeNull();
  });

  it('is never true without a person on the page or an account', () => {
    const u = account({ is_patient: true, person_id: 7 });
    expect(isOwnRecord(u, undefined)).toBe(false);
    expect(isOwnRecord(u, '')).toBe(false);
    expect(isOwnRecord(null, '7')).toBe(false);
    expect(isOwnRecord(undefined, undefined)).toBe(false);
  });
});

describe('ownRecordPath (#2838)', () => {
  const link = { role: 'patient', scope: 'patient', source: 'patient_link', person_id: 12, expires_at: null };
  const account = (extra: Record<string, unknown>): User =>
    ({ id: 1, email: 'u@test.com', ...extra }) as unknown as User;

  it("is home for a patient-only account, whose home is its record", () => {
    expect(ownRecordPath(account({ is_patient: true, person_id: 7 }))).toBe('/');
  });

  it('is the record page for an account that is also staff, an org admin or a doctor', () => {
    expect(ownRecordPath(account({ is_staff: true, effective_roles: [link] }))).toBe('/patient/12');
    expect(ownRecordPath(account({ is_org_admin: true, effective_roles: [link] }))).toBe('/patient/12');
    expect(ownRecordPath(account({ org_accesses: [grant('doctor')], effective_roles: [link] }))).toBe('/patient/12');
  });

  it('is nothing for an analyst-only account, or one with no record', () => {
    expect(ownRecordPath(account({ org_accesses: [grant('analyst')], effective_roles: [link] }))).toBeNull();
    expect(ownRecordPath(account({ is_staff: true }))).toBeNull();
    expect(ownRecordPath(null)).toBeNull();
  });
});
