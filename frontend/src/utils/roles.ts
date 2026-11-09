import type { User } from '@/hooks/useAuth';

/**
 * What a signed-in professional may reach, mirroring the server's
 * `has_curation_access` / `has_professional_access` (omop_core/services/access.py).
 *
 * The API enforces every rule independently; these only decide what the UI
 * offers. Three roles, three reaches:
 *
 * - staff and org_admin (a grant, or `is_org_admin` through a trust): everything;
 * - doctor: every curation page, and patient writes;
 * - analyst: the Code Mapping page (both tabs, the dialog, Suggest, Lock, Done,
 *   the accuracy pages) and read-only patient viewing -- nothing else (#2398).
 */
const CURATION_ROLES = ['org_admin', 'doctor'];
const PROFESSIONAL_ROLES = ['org_admin', 'doctor', 'analyst'];

const hasGrant = (user: User | null | undefined, roles: string[]): boolean =>
  !!user?.org_accesses?.some((a) => roles.includes(a.role ?? ''));

/** Approves mappings, administers organizations, sees a patient's Source
 *  Codes and OMOP tabs: staff, or org_admin by grant or trust. The server's
 *  `_can_approve_mappings` / `has_org_admin_access`. */
export function isStaffOrOrgAdmin(user: User | null | undefined): boolean {
  return !!(user?.is_staff || user?.is_org_admin);
}

/** Field mappings, therapy and disease-category reference, relevance signals,
 *  source systems, the mapping hub, uploads: staff, org_admin or doctor. */
export function hasCurationRole(user: User | null | undefined): boolean {
  return isStaffOrOrgAdmin(user) || hasGrant(user, CURATION_ROLES);
}

/** The Code Mapping page: every professional role, the analyst included. */
export function hasCodeMappingRole(user: User | null | undefined): boolean {
  return hasCurationRole(user) || hasGrant(user, PROFESSIONAL_ROLES);
}

/** A professional whose only reach is the analyst's: patient pages are read-only. */
export function isReadOnlyAnalyst(user: User | null | undefined): boolean {
  return hasCodeMappingRole(user) && !hasCurationRole(user);
}
