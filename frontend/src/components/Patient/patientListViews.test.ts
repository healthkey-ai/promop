import { beforeEach, describe, expect, it } from 'vitest';

import { DEFAULT_ORDERING, columnsFor, loadPreferences, REVIEW_FILTER_DEFAULTS } from './patientListViews';

const KEY = 'promop-patient-list:test';

describe('loadPreferences', () => {
  beforeEach(() => localStorage.clear());

  it('carries over the selected audience preset of a record saved before #2470', () => {
    localStorage.setItem(KEY, JSON.stringify({ view: 'foundation', views: {
      foundation: { filters: { ecog: '2' }, ordering: '-gaps' }, doctor: { filters: { ecog: '0' }, ordering: 'name' },
    } }));
    expect(loadPreferences(KEY)).toEqual({ filters: { ...REVIEW_FILTER_DEFAULTS, ecog: '2' }, ordering: '-gaps', disease_category: 'all', disease: 'all' });
  });

  it.each([
    ['an unselected preset only', { view: 'analyst', views: { doctor: { ordering: '-gaps' } } }],
    ['a prototype name as the view', { view: 'constructor', views: {} }],
    ['garbage', 'x'],
    ['an array', []],
    ['null', null],
  ])('yields the defaults for %s', (_label, saved) => {
    localStorage.setItem(KEY, JSON.stringify(saved));
    expect(loadPreferences(KEY)).toEqual({ filters: { ...REVIEW_FILTER_DEFAULTS }, ordering: DEFAULT_ORDERING, disease_category: 'all', disease: 'all' });
  });

  it('drops an ordering no heading offers and an unknown filter choice', () => {
    localStorage.setItem(KEY, JSON.stringify({ filters: { ecog: '9', contact: 'missing' }, ordering: 'status' }));
    expect(loadPreferences(KEY)).toEqual({ filters: { ...REVIEW_FILTER_DEFAULTS, contact: 'missing' }, ordering: DEFAULT_ORDERING, disease_category: 'all', disease: 'all' });
  });

  it('keeps the Disease Category and Disease, and a Disease only inside a category', () => {
    localStorage.setItem(KEY, JSON.stringify({ disease_category: 'PC', disease: 'Prostate cancer' }));
    expect(loadPreferences(KEY)).toMatchObject({ disease_category: 'PC', disease: 'Prostate cancer' });
    localStorage.setItem(KEY, JSON.stringify({ disease: 'Prostate cancer' }));
    expect(loadPreferences(KEY)).toMatchObject({ disease_category: 'all', disease: 'all' });
    localStorage.setItem(KEY, JSON.stringify({ disease_category: 7, disease: ['x'] }));
    expect(loadPreferences(KEY)).toMatchObject({ disease_category: 'all', disease: 'all' });
  });
});

describe('columnsFor', () => {
  it('adds outreach and organization columns only when asked', () => {
    expect(columnsFor({ outreach: false, multiOrg: false })).toEqual(['treatment', 'ecog', 'gaps', 'freshness']);
    expect(columnsFor({ outreach: true, multiOrg: true })).toEqual(['treatment', 'ecog', 'organization', 'location', 'contact', 'gaps', 'freshness']);
  });
});
