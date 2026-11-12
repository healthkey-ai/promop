/**
 * What each role reaches, in the user's words. Mirrors the role table in
 * AGENTS.md ("Roles and what each reaches") and the server helpers in
 * omop_core/services/access.py; the API enforces every rule on its own, this
 * only explains it (#2473). Roles are additive: a user holds one grant per
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
      'Hold admin authority at organizations that trust this one or its email domain.',
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
      'Read and edit the patients of the groups the grant names, or of a public demo organization.',
      'Open every mapping page: the hub, Code Mapping, field mappings, therapy and disease-category reference, relevance signals, source systems.',
    ],
    cannot: [
      'Create, edit or propose a mapping, or run Suggest; that needs the Analyst role as well.',
      'Approve a mapping; that stays with org admins and staff.',
      'Administer access or organization settings.',
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
      'See any patient\'s data: the patient list is empty, a record is not found, clinical rows and source codes are out of reach.',
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
      'At a public demo organization, browse the sample patients de-identified.',
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
