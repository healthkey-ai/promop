export const REVIEW_FILTER_DEFAULTS = {
  clinical_status: 'all', ecog: 'all', data_gap: 'all', freshness: 'all',
  treatment: '', biomarker: '', location: '', contact: 'all',
};
export type ReviewFilters = typeof REVIEW_FILTER_DEFAULTS;
export interface Preferences {
  filters: ReviewFilters;
  ordering: string;
}
export const DEFAULT_ORDERING = '-updated';
const ORDERINGS = /^-?(updated|age|name|disease|stage|ecog|lines|status|unmapped_source_codes|freshness|gaps|organization)$/;

/** The columns beside the base set follow the reader's roles, not a chosen
 *  preset (#2463). Clinical context (latest line, ECOG) for everyone who
 *  reaches the list; the outreach columns (location, whether contact
 *  information is recorded) for staff and org admins, whose work spans the
 *  org's patients; Organization only when the list spans more than one. */
export function columnsFor({ outreach, multiOrg }: { outreach: boolean; multiOrg: boolean }): readonly string[] {
  return [
    'treatment', 'ecog',
    ...(multiOrg ? ['organization'] : []),
    ...(outreach ? ['location', 'contact'] : []),
    'gaps', 'freshness',
  ];
}

function sanitize(source: unknown): Preferences | null {
  if (!source || typeof source !== 'object') return null;
  const raw = source as { filters?: Record<string, unknown>; ordering?: unknown };
  const filters = { ...REVIEW_FILTER_DEFAULTS };
  for (const field of Object.keys(filters) as (keyof ReviewFilters)[]) {
    const value = raw.filters?.[field];
    if (typeof value !== 'string') continue;
    const choices: Partial<Record<keyof ReviewFilters, string[]>> = {
      ecog: ['all', 'unknown', '0', '1', '2', '3', '4', '5'],
      data_gap: ['all', 'any', 'none', 'stage', 'ecog', 'genomics'],
      freshness: ['all', 'unknown', '30d', '90d', 'older'], contact: ['all', 'available', 'missing'],
    };
    if (!choices[field] || choices[field]?.includes(value)) filters[field] = value.slice(0, 256);
  }
  return { filters, ordering: typeof raw.ordering === 'string' && ORDERINGS.test(raw.ordering) ? raw.ordering : DEFAULT_ORDERING };
}

/** Clinical filters and sort, saved per account in this browser. A record
 *  saved by the earlier audience presets ({view, views: {doctor: ...}})
 *  carries over the preset that was selected. */
export function loadPreferences(key: string): Preferences {
  try {
    const saved = JSON.parse(localStorage.getItem(key) || '{}');
    const legacy = saved?.views && saved?.view ? saved.views[saved.view] : null;
    return sanitize(legacy ?? saved) ?? { filters: { ...REVIEW_FILTER_DEFAULTS }, ordering: DEFAULT_ORDERING };
  } catch {
    return { filters: { ...REVIEW_FILTER_DEFAULTS }, ordering: DEFAULT_ORDERING };
  }
}
