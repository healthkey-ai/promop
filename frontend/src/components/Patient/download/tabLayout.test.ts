/**
 * The PDF's layout is generated from the tabs. These tests keep it honest:
 * the generated file must match a fresh extraction, and the extraction must see
 * every field the duplicate-field lint rule sees — the rule reads the same tabs
 * through the same AST, so a field one finds and the other misses means a tab
 * grew a shape the extractor does not know.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { Linter } from 'eslint';
import tsParser from '@typescript-eslint/parser';
import { describe, it, expect } from 'vitest';
// @ts-expect-error -- plain JS build script, no declarations
import { extractAll, render, OUTPUT, TAB_DIR, TAB_FILES } from '../../../../scripts/tab-layout.mjs';
// @ts-expect-error -- plain JS lint rule, no declarations
import rule, { __resetTabFieldState, __tabFieldsByFile } from '../../../../eslint-rules/no-duplicate-tab-field.js';
import { ASSEMBLED_FIELDS, DISEASE_COMPONENTS } from './patientReport';
import { TAB_LAYOUT } from './tabLayout.generated';

describe('tab layout', () => {
  it('retains recorded categories after the explicit-answer editor replaces the generic field', () => {
    const history = extractAll().GeneralTab.find((section: { title: string }) => section.title === 'Medical History');
    expect(history.fields).toContainEqual(['Pre-existing Conditions', 'preexisting_conditions']);
  });

  it('is current: run `node scripts/tab-layout.mjs` after changing a tab', () => {
    expect(readFileSync(OUTPUT, 'utf8')).toBe(render(extractAll()));
  });

  it('holds, tab by tab, every field the duplicate-field rule sees', () => {
    __resetTabFieldState();
    const linter = new Linter();
    const config = {
      files: ['**/*.tsx'],
      plugins: { promop: { rules: { 'no-duplicate-tab-field': rule } } },
      languageOptions: { parser: tsParser, parserOptions: { ecmaFeatures: { jsx: true } } },
      rules: { 'promop/no-duplicate-tab-field': 'error' },
    };
    // Genomics prints as an assembled table, not from the layout, but its field
    // still has to be accounted for.
    const linted = [...(TAB_FILES as string[]), 'GenomicsTab.tsx'];
    for (const file of linted) {
      const messages = linter.verify(readFileSync(join(TAB_DIR, file), 'utf8'), config as never, join(TAB_DIR, file));
      // A tab the rule saw no field on, or a field it could not name, is one
      // this comparison cannot vouch for either: both readers would be blind.
      expect(messages.map((m) => `${file}: ${m.messageId ?? m.message}`)).toEqual([]);
    }
    const seen = __tabFieldsByFile() as Map<string, Set<string>>;

    const missing: string[] = [];
    for (const file of linted) {
      const tab = file.replace(/\.tsx$/, '');
      const keys = seen.get(file);
      expect(keys?.size, `${file} has no fields the rule can see`).toBeGreaterThan(0);
      // Per tab, not pooled: a field that left one tab but exists on another
      // still has to be printed under the tab that shows it.
      const printed = new Set<string>([
        ...(TAB_LAYOUT[tab] ?? []).flatMap((s) => s.fields.map(([, n]) => n)),
        ...ASSEMBLED_FIELDS,
      ]);
      for (const key of keys ?? []) if (!printed.has(key)) missing.push(`${tab}.${key}`);
    }
    expect(missing).toEqual([]);
  });

  it('maps every Disease tab component to a disease', () => {
    const mapped = new Set(Object.values(DISEASE_COMPONENTS).flat());
    const components = new Set(TAB_LAYOUT.DiseaseTab.map((s) => s.component));
    expect([...components].filter((c) => !mapped.has(c))).toEqual([]);
  });
});
