/**
 * What each role reaches, in the user's words. Follows the server helpers in
 * omop_core/services/access.py (has_curation_access, has_mapping_read_access,
 * has_mapping_write_access, get_admin_access_paths) and _can_manage_mappings
 * in patient_portal/api/views.py, summarized in AGENTS.md ("Roles and what
 * each reaches"); the API enforces every rule on its own, this only explains
 * it (#2473). Roles are additive: a user holds one grant per
 * role at an organization, and each grant adds its own reach.
 */
export interface RoleDescription {
  key: string;
  label: string;
  /** One line, for a tooltip. */
  summary: string;
  /** What the role can do. */
  can: string[];
  /** What it deliberately cannot. */
  cannot: string[];
}

export const ROLE_DESCRIPTIONS: RoleDescription[] = [
  {
    key: 'org_admin',
    label: 'Org Admin',
    summary: 'Everything for the organization: patients, mappings and their approval, access, uploads. Includes what Doctor and Analyst can do.',
    can: [
      'Read and edit every patient of the organization, and upload patient data.',
      'Open every mapping page, create and change mappings, and approve them.',
      'Administer access: invite users, grant and revoke roles, manage trusts and organization settings.',
      'Hold admin authority at organizations that trust this one or its email domain (a grant at a public demo organization does not carry).',
    ],
    cannot: [
      'Nothing is held back; an org admin needs no other role.',
    ],
  },
  {
    key: 'doctor',
    label: 'Doctor',
    summary: 'Reads and edits patients; sees every mapping page read-only.',
    can: [
      'Read and edit patients: those of the groups the grant names are listed; an organization-level grant opens the organization\'s patients by id but lists none, except at a public demo organization, whose sample patients every grant there lists.',
      'Open every mapping page: the hub, Code Mapping, field mappings, therapy and disease-category reference, relevance signals, source systems.',
      'Hold admin authority at organizations that trust this one or its email domain, as an org admin does (a grant at a public demo organization does not carry).',
    ],
    cannot: [
      'Create, edit or propose a mapping, or run Suggest; that needs the Analyst role as well.',
      'Approve a mapping; that stays with org admins and staff.',
      'Administer access or organization settings at this organization; that is the Org Admin role.',
    ],
  },
  {
    key: 'analyst',
    label: 'Analyst',
    summary: 'Works on code and field mappings and proposes them; sees no individual patient data.',
    can: [
      'Open every mapping page and write there: create, edit, propose and upload code and field mappings, synonyms, choices and custom fields; run Suggest; Lock, Done and priority; axis variants and LOINC axes.',
      'Use PRism analytics.',
      'Browse a public demo organization\'s sample patients, de-identified: no names, no demographics, no detail page.',
    ],
    cannot: [
      'See any patient\'s data: outside a public demo organization the patient list is empty, a record is not found, and clinical rows and source codes are out of reach.',
      'Approve a mapping; that stays with org admins and staff.',
      'Gain anything from an organization trust.',
    ],
  },
  {
    key: 'patient',
    label: 'Patient',
    summary: 'Their own record only.',
    can: [
      'Read and update their own record, lab results and documents.',
    ],
    cannot: [
      'See another patient\'s record, or any mapping or administration page.',
    ],
  },
];

export const ROLE_BY_KEY: Record<string, RoleDescription> = Object.fromEntries(
  ROLE_DESCRIPTIONS.map((role) => [role.key, role]),
);

export const ROLE_LABELS: Record<string, string> = {
  staff: 'Staff',
  ...Object.fromEntries(ROLE_DESCRIPTIONS.map((role) => [role.key, role.label])),
};

export const ROLES_PAGE_PATH = '/roles';
