import type { User } from '@/hooks/useAuth';

/**
 * What a signed-in professional may reach, mirroring the server's role
 * helpers in omop_core/services/access.py (`has_curation_access`,
 * `has_code_mapping_access`, `has_org_admin_access`).
 *
 * The API enforces every rule independently; these only decide what the UI
 * offers. Roles are additive (#2416): a user holds one grant per role at an
 * org, and each grant adds its own reach.
 *
 * - staff and org_admin (a grant, or `is_org_admin` through a trust): everything;
 * - doctor: patients (read and write) and every curation page except Code Mapping;
 * - analyst: the Code Mapping page (both tabs, the dialog, Suggest, Lock, Done,
 *   the accuracy pages) and PRism analytics. No patient data at all.
 */
const CURATION_ROLES = ['org_admin', 'doctor'];
const CODE_MAPPING_ROLES = ['analyst'];

const hasGrant = (user: User | null | undefined, roles: string[]): boolean =>
  !!user?.org_accesses?.some((a) => roles.includes(a.role ?? ''));

/** Approves mappings, administers organizations, sees a patient's Source
 *  Codes and OMOP tabs: staff, or org_admin by grant or trust. The server's
 *  `_can_approve_mappings` / `has_org_admin_access`. */
export function isStaffOrOrgAdmin(user: User | null | undefined): boolean {
  return !!(user?.is_staff || user?.is_org_admin);
}

/** Patients, field mappings, therapy and disease-category reference,
 *  relevance signals, source systems, the mapping hub, uploads: staff,
 *  org_admin or doctor. */
export function hasCurationRole(user: User | null | undefined): boolean {
  return isStaffOrOrgAdmin(user) || hasGrant(user, CURATION_ROLES);
}

/** The Code Mapping page: staff, org_admin, or an analyst grant. A doctor
 *  without one does not map codes (#2416). */
export function hasCodeMappingRole(user: User | null | undefined): boolean {
  return isStaffOrOrgAdmin(user) || hasGrant(user, CODE_MAPPING_ROLES);
}

/** A professional whose only reach is the analyst's: no patient pages, so
 *  the app opens on Code Mapping instead of the patient list. */
export function isAnalystOnly(user: User | null | undefined): boolean {
  return hasCodeMappingRole(user) && !hasCurationRole(user) && !user?.is_patient;
}
