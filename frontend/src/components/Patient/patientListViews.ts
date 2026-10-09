export type PatientView = 'doctor' | 'foundation' | 'analyst';
export const REVIEW_FILTER_DEFAULTS = {
  clinical_status: 'all', ecog: 'all', data_gap: 'all', freshness: 'all',
  treatment: '', biomarker: '', location: '', contact: 'all',
};
export type ReviewFilters = typeof REVIEW_FILTER_DEFAULTS;
export const VIEW_COLUMNS = {
  doctor: ['treatment', 'status', 'ecog', 'gaps', 'freshness'],
  foundation: ['status', 'location', 'contact', 'gaps', 'freshness'],
  analyst: ['status', 'ecog', 'organization', 'location', 'gaps', 'freshness'],
} as const;
export interface SavedView {
  filters: ReviewFilters;
  ordering: string;
}
interface Preferences {
  view: PatientView;
  views: Partial<Record<PatientView, SavedView>>;
}
export function loadPreferences(key: string): Preferences {
  try {
    const saved = JSON.parse(localStorage.getItem(key) || '{}');
    const view = Object.prototype.hasOwnProperty.call(VIEW_COLUMNS, saved.view) ? saved.view as PatientView : 'doctor';
    const views: Preferences['views'] = {};
    for (const name of Object.keys(VIEW_COLUMNS) as PatientView[]) {
      const source = saved.views?.[name];
      if (!source) continue;
      const filters = { ...REVIEW_FILTER_DEFAULTS };
      for (const field of Object.keys(filters) as (keyof ReviewFilters)[]) {
        const value = source.filters?.[field];
        if (typeof value !== 'string') continue;
        const choices: Partial<Record<keyof ReviewFilters, string[]>> = {
          ecog: ['all', 'unknown', '0', '1', '2', '3', '4', '5'],
          data_gap: ['all', 'any', 'none', 'stage', 'ecog', 'genomics'],
          freshness: ['all', 'unknown', '30d', '90d', 'older'], contact: ['all', 'available', 'missing'],
        };
        if (!choices[field] || choices[field]?.includes(value)) filters[field] = value.slice(0, 256);
      }
      views[name] = { filters, ordering: typeof source.ordering === 'string' && /^-?(updated|age|name|disease|stage|ecog|lines|status|freshness|gaps|organization)$/.test(source.ordering) ? source.ordering : '-updated' };
    }
    return { view, views };
  } catch {
    return { view: 'doctor', views: {} };
  }
}
