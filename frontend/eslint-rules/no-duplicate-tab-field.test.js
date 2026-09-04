/**
 * Tests for the rule that keeps one PatientInfo field to one tab.
 *
 * The guard this rule replaced was wrong in five distinct ways and passed its
 * own tests every time, so what matters here is not that the rule reports on a
 * duplicate — it is that it reports on the *shapes the tabs actually use*, and
 * stays quiet on the shapes that merely look alike. Each case below is one of
 * those, and several exist because an earlier regex version got them wrong.
 */
import { Linter } from 'eslint';
import tsParser from '@typescript-eslint/parser';
import { describe, it, expect, beforeEach } from 'vitest';
import rule, { __resetTabFieldState } from './no-duplicate-tab-field.js';

const linter = new Linter();
const CONFIG = {
  // Without a `files` pattern a flat config applies to .js only, and every
  // fixture below comes back "No matching configuration found" — messages with
  // no messageId, which read as reports until you look at them.
  files: ['**/*.tsx'],
  plugins: { promop: { rules: { 'no-duplicate-tab-field': rule } } },
  languageOptions: {
    parser: tsParser,
    parserOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      ecmaFeatures: { jsx: true },
    },
  },
  rules: { 'promop/no-duplicate-tab-field': 'error' },
};

/** Lint files in order, as one `eslint .` run would, and collect the reports. */
const lint = (files) =>
  files.flatMap(([filename, code]) =>
    linter
      .verify(code, CONFIG, filename)
      .map((m) => ({ file: filename, id: m.messageId, text: m.message })),
  );

const ids = (files) => lint(files).map((m) => m.id);

/** A tab whose fields are declared inline, with an arrow-function prop. */
const inlineTab = (...keys) => `
  export default function Tab({ formData, onChange }) {
    return (<div>
      ${keys.map((k) => `<ClinicalField label="L" onChange={(f, v) => onChange(f, v)} name="${k}" value={formData?.${k}} />`).join('\n')}
    </div>);
  }`;

/** A tab whose fields live in a table that a section() renders. */
const tableTab = (...keys) => `
  const COUNTS: Array<[string, string]> = [
    ${keys.map((k) => `['L', '${k}']`).join(',\n')}
  ];
  export default function Tab({ formData, onChange }) {
    const section = (title: string, fields: Array<[string, string]>) => (
      <Section title={title}>
        {fields.map(([label, name]) => (
          <ClinicalField key={name} label={label} name={name}
            value={formData?.[name]} onChange={onChange} />
        ))}
      </Section>
    );
    return <div>{section('Counts', COUNTS)}</div>;
  }`;

beforeEach(__resetTabFieldState);

describe('no-duplicate-tab-field', () => {
  it('catches a duplicate declared inline on both tabs', () => {
    expect(ids([
      ['ATab.tsx', inlineTab('psa_ng_ml')],
      ['BTab.tsx', inlineTab('psa_ng_ml')],
    ])).toEqual(['duplicate']);
  });

  it('catches one declared in a field table', () => {
    // A `>` inside `onChange={(f, v) => onChange(f, v)}` ended the element early
    // for the regex guard, so a real duplicate of exactly this shape passed.
    expect(ids([
      ['ATab.tsx', tableTab('ldh_u_l')],
      ['BTab.tsx', inlineTab('ldh_u_l')],
    ])).toEqual(['duplicate']);
  });

  it('catches one declared with field()', () => {
    expect(ids([
      ['ATab.tsx', inlineTab('stage_group')],
      ['BTab.tsx', "export default () => <div>{field('Stage', 'stage_group', 'select')}</div>;"],
    ])).toEqual(['duplicate']);
  });

  it('names both tabs, since the report lands on whichever is linted second', () => {
    const [report] = lint([
      ['ATab.tsx', inlineTab('psa_ng_ml')],
      ['BTab.tsx', inlineTab('psa_ng_ml')],
    ]);
    expect(report.file).toBe('BTab.tsx');
    expect(report.text).toContain('ATab.tsx');
  });

  it('ignores an array two tabs share that no section renders', () => {
    // An options list is declared exactly like a field table. Reporting one
    // would fail the build over work with nothing to do with field ownership.
    const options = (key) => `
      const YES_NO: Array<[string, string]> = [['Yes', 'yes'], ['No', 'no']];
      export default () => (<div>
        <Select options={YES_NO} />
        <ClinicalField name="${key}" />
      </div>);`;
    expect(ids([
      ['ATab.tsx', options('a_field')],
      ['BTab.tsx', options('b_field')],
    ])).toEqual([]);
  });

  it('reads a table handed to section() as a literal', () => {
    expect(ids([
      ['ATab.tsx', `export default () => {
        const section = (t, fs) => <div>{fs.map(([l, name]) =>
          <ClinicalField key={name} name={name} />)}</div>;
        return section('Counts', [['L', 'ldh_u_l']]);
      };`],
      ['BTab.tsx', inlineTab('ldh_u_l')],
    ])).toEqual(['duplicate']);
  });

  it('reports a field table it cannot follow instead of skipping it', () => {
    // An alias, an import or a call. Every field behind it would otherwise be
    // exempt from the duplicate check in silence.
    expect(ids([['ATab.tsx', `
      const COUNTS: Array<[string, string]> = [['L', 'ldh_u_l']];
      const ALIAS = COUNTS;
      export default () => {
        const section = (t, fs) => <div>{fs.map(([l, name]) =>
          <ClinicalField key={name} name={name} />)}</div>;
        return section('Counts', ALIAS);
      };`]])).toEqual(['unreadableTable']);
  });

  it('reports a table row whose key it cannot read', () => {
    // The tab's other rows keep the noFields check quiet, so saying nothing
    // here would hide the duplicate outright rather than merely under-report.
    expect(ids([['ATab.tsx', `
      const K = {a: 'ldh_u_l'};
      const COUNTS: Array<[string, string]> = [['Readable', 'plain'], ['Hidden', K.a]];
      export default () => {
        const section = (t, fs) => <div>{fs.map(([l, name]) =>
          <ClinicalField key={name} name={name} />)}</div>;
        return section('Counts', COUNTS);
      };`]])).toEqual(['unreadableRow']);
  });

  it('reports a field() key it cannot read', () => {
    expect(ids([[
      'ATab.tsx',
      "const K = {a: 'x'}; export default () => <div>{field('L', K.a, 'select')}</div>;",
    ]])).toEqual(['unreadableFieldCall']);
  });

  it('reports a tab it can see no field on at all', () => {
    // The residual hole a completeness report cannot cover: rename the helper
    // and the rule finds nothing, with nothing unreadable to complain about.
    // Every field on the tab would be silently exempt.
    expect(ids([['ATab.tsx', `
      const COUNTS: Array<[string, string]> = [['L', 'ldh_u_l']];
      export default () => {
        const group = (t, fs) => <div>{fs.map(([l, name]) =>
          <ClinicalField key={name} name={name} />)}</div>;
        return group('Counts', COUNTS);
      };`]])).toEqual(['noFields']);
  });

  it('says one thing, not two, about a tab whose only table is unreadable', () => {
    const reports = ids([['ATab.tsx', `
      export default () => {
        const section = (t, fs) => <div>{fs.map(([l, name]) =>
          <ClinicalField key={name} name={name} />)}</div>;
        return section('Counts', somewhereElse());
      };`]]);
    expect(reports).toEqual(['unreadableTable']);
  });

  it('allows one field twice on a single tab', () => {
    // DiseaseTab renders the same field under several mutually exclusive
    // disease branches. One tab, one box on screen — not a duplicate.
    expect(ids([['ATab.tsx', inlineTab('histology', 'histology')]])).toEqual([]);
  });

  it('accepts name={name}, the field-table loop variable', () => {
    expect(ids([['ATab.tsx', tableTab('hemoglobin_g_dl')]])).toEqual([]);
  });

  it('reports a computed name rather than skipping it in silence', () => {
    // Silently skipping what it cannot read is how a guard reports green over
    // the bug it exists for.
    expect(ids([
      ['ATab.tsx', 'export default () => <ClinicalField name={KEYS[0]} />;'],
    ])).toEqual(['unreadable']);
  });

  it('exempts the known duplicates it is configured with', () => {
    // Only 'duplicate' is in question here. This fixture renders none of the
    // other configured entries, so it also reports those stale — correctly, and
    // the next test is what covers that.
    const reports = ids([
      ['DiseaseTab.tsx', inlineTab('stage')],
      ['GeneralTab.tsx', inlineTab('stage')],
    ]);
    expect(reports.filter((id) => id === 'duplicate')).toEqual([]);
  });

  it('flags a known duplicate that no longer renders on both', () => {
    // A fixed duplicate must not leave a permanent hole behind it — whichever
    // half drops the field, and whichever order the two are linted in. All
    // three configured entries pair the same two tabs, so a fixture that
    // renders none of them makes all three stale, which is the point.
    const stale = (files) => {
      __resetTabFieldState();
      const reports = lint(files);
      expect(reports.map((r) => r.id)).toEqual(['stale', 'stale', 'stale']);
      return reports.map((r) => /'([a-z_]+)' is listed/.exec(r.text)[1]).sort();
    };

    const expected = ['disease', 'histologic_type', 'stage'];
    expect(stale([
      ['DiseaseTab.tsx', inlineTab('stage')],
      ['GeneralTab.tsx', inlineTab('something_else')],
    ])).toEqual(expected);
    expect(stale([
      ['DiseaseTab.tsx', inlineTab('something_else')],
      ['GeneralTab.tsx', inlineTab('stage')],
    ])).toEqual(expected);
  });

  it('does not run on files that are not tabs', () => {
    expect(ids([
      ['ATab.tsx', inlineTab('psa_ng_ml')],
      ['Helper.tsx', inlineTab('psa_ng_ml')],
    ])).toEqual([]);
  });
});
