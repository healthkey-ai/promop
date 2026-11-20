import type { User } from '@/hooks/useAuth';

/**
 * What a signed-in professional may reach, mirroring the server's role
 * helpers in omop_core/services/access.py (`has_curation_access`,
 * `has_mapping_read_access`, `has_mapping_write_access`,
 * `has_org_admin_access`).
 *
 * The API enforces every rule independently; these only decide what the UI
 * offers. Roles are additive (#2416): a user holds one grant per role at an
 * org, and each grant adds its own reach.
 *
 * - staff and org_admin (a grant, or `is_org_admin` through a trust): everything;
 * - doctor: patients (read and write), and every mapping page read-only;
 * - analyst: every mapping page, writing (create, edit, propose, Suggest,
 *   Lock, Done, priority, upload) and PRism analytics. No patient data,
 *   except the sample patients of a public demo org it holds a grant at: a
 *   demo org lists them to every grant there, whatever its role (#1392).
 */
const PATIENT_ROLES = ['org_admin', 'doctor'];
const MAPPING_WRITE_ROLES = ['analyst'];
const PROFESSIONAL_ROLES = ['org_admin', 'doctor', 'analyst'];

const hasGrant = (user: User | null | undefined, roles: string[]): boolean =>
  !!user?.org_accesses?.some((a) => roles.includes(a.role ?? ''));

/** Approves mappings, administers organizations, sees a patient's Source
 *  Codes and OMOP tabs: staff, or org_admin by grant or trust. The server's
 *  `_can_approve_mappings` / `has_org_admin_access`. */
export function isStaffOrOrgAdmin(user: User | null | undefined): boolean {
  return !!(user?.is_staff || user?.is_org_admin);
}

/** Holds a professional grant at a public demo org, which lists its sample
 *  patients to any role (`PatientRecordViewSet.get_queryset`). */
const hasDemoOrgGrant = (user: User | null | undefined): boolean =>
  !!user?.org_accesses?.some(
    (a) => a.org_allows_patient_signup && PROFESSIONAL_ROLES.includes(a.role ?? ''),
  );

/** Reads a patient's data: opens the detail page, sees names and other
 *  demographics on the list. Staff, org_admin or doctor, the server's
 *  `has_curation_access`. */
export function canOpenPatients(user: User | null | undefined): boolean {
  return isStaffOrOrgAdmin(user) || hasGrant(user, PATIENT_ROLES);
}

/** Reaches the patient list: anyone who opens patients, or any professional
 *  grant at a public demo org, whose list every grant there sees. A reader
 *  who cannot open patients gets the rows de-identified (#2460). */
export function canReadPatients(user: User | null | undefined): boolean {
  return canOpenPatients(user) || hasDemoOrgGrant(user);
}

/** Opens the mapping pages (the hub, Code Mapping, field mappings, therapy
 *  and disease-category reference, relevance signals, source systems):
 *  staff, or any professional grant. A doctor reads them (#2416). */
export function canViewMappings(user: User | null | undefined): boolean {
  return isStaffOrOrgAdmin(user) || hasGrant(user, PROFESSIONAL_ROLES);
}

/** Creates or changes a mapping: staff, org_admin, or an analyst grant. A
 *  doctor without one reads the mapping pages but writes nothing (#2416). */
export function canWriteMappings(user: User | null | undefined): boolean {
  return isStaffOrOrgAdmin(user) || hasGrant(user, MAPPING_WRITE_ROLES);
}

/** A professional whose only reach is the analyst's: no patient pages, so
 *  the app opens on Code Mapping instead of the patient list. */
export function isAnalystOnly(user: User | null | undefined): boolean {
  return canWriteMappings(user) && !canReadPatients(user) && !user?.is_patient;
}
