/**
 * Read the section / label / field layout out of the patient tabs.
 *
 * The PDF download prints what the tabs show, in their order and under their
 * labels. Copying those by hand would drift the first time a tab gained a
 * field, so they are read from the tabs' own source and written to
 * `src/components/Patient/download/tabLayout.generated.ts`:
 *
 *     node scripts/tab-layout.mjs
 *
 * `tabLayout.test.ts` re-runs the extraction and fails when the generated file
 * is stale, so a tab edit without a regeneration is caught in CI.
 *
 * It reads the AST, not text, for the reason `eslint-rules/no-duplicate-tab-field.js`
 * gives, and it recognises the same three shapes that rule does:
 * `<ClinicalField label="…" name="…">`, `field('Label', 'name', …)` and
 * `section('Title', TABLE)` over `[['Label', 'name'], …]` rows.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from '@typescript-eslint/parser';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
export const TAB_DIR = join(ROOT, 'src/components/PatientInfo/tabs');
export const OUTPUT = join(ROOT, 'src/components/Patient/download/tabLayout.generated.ts');

/** The tabs the PDF prints, in PatientDetail's order. */
export const TAB_FILES = [
  'GeneralTab.tsx', 'DiseaseTab.tsx', 'TreatmentTab.tsx',
  'LabsTab.tsx', 'BehaviorTab.tsx', 'WearableTab.tsx',
];

const ENTITIES = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'" };
const decode = (s) => s.replace(/&(amp|lt|gt|quot|#39);/g, (m) => ENTITIES[m]);

const literal = (node) =>
  node?.type === 'Literal' && typeof node.value === 'string' ? decode(node.value) : null;

const attr = (opening, name) => {
  const a = opening.attributes.find((x) => x.type === 'JSXAttribute' && x.name.name === name);
  if (!a?.value) return null;
  if (a.value.type === 'Literal') return literal(a.value);
  if (a.value.type === 'JSXExpressionContainer') return literal(a.value.expression);
  return null;
};

/** `!patientMode && …`: the tab hides this from the patient themself. */
const isStaffOnlyGuard = (node) =>
  node.type === 'LogicalExpression' && node.operator === '&&'
  && node.left.type === 'UnaryExpression' && node.left.operator === '!'
  && node.left.argument.type === 'Identifier' && node.left.argument.name === 'patientMode';

/**
 * @returns {{title: string, component: string, staffOnly?: true, fields: [string, string][]}[]}
 */
export function extractTabLayout(source) {
  const ast = parse(source, { jsx: true, ecmaVersion: 'latest', sourceType: 'module' });
  const tables = new Map();
  const sections = [];
  let current = null;

  const open = (title, component, staffOnly) => {
    const section = { title, component, ...(staffOnly ? { staffOnly: true } : {}), fields: [] };
    sections.push(section);
    return section;
  };
  const add = (section, label, name) => {
    if (!section || !label || !name) return;
    // A field offered twice in one section is one value: `region` is a State
    // select in the US and free text elsewhere. The first label stands, and
    // patientReport.ts relabels `region` the way the tab does.
    if (!section.fields.some(([, n]) => n === name)) section.fields.push([label, name]);
  };

  const walk = (node, ctx) => {
    if (!node || typeof node.type !== 'string') return;

    if (node.type === 'VariableDeclarator' && node.id.type === 'Identifier'
        && node.init?.type === 'ArrayExpression') {
      tables.set(node.id.name, node.init);
    }

    let next = ctx;
    if (node.type === 'FunctionDeclaration' && node.id && ctx.component === null) {
      next = { ...ctx, component: node.id.name };
    }
    if (isStaffOnlyGuard(node)) next = { ...next, staffOnly: true };

    if (node.type === 'JSXElement' && node.openingElement.name.name === 'Section') {
      const title = attr(node.openingElement, 'title');
      if (title) {
        const saved = current;
        current = open(title, next.component, next.staffOnly);
        for (const child of node.children) walk(child, next);
        current = saved;
        return;
      }
    }
    if (node.type === 'JSXOpeningElement' && node.name.name === 'ClinicalField') {
      add(current, attr(node, 'label'), attr(node, 'name'));
    }
    // The deliberate-answer editor replaces the generic category field, but
    // those recorded categories still belong in the downloaded medical history.
    if (node.type === 'JSXOpeningElement' && node.name.name === 'PreexistingConditionsAnswer') {
      add(current, 'Pre-existing Conditions', 'preexisting_conditions');
    }
    if (node.type === 'CallExpression' && node.callee.type === 'Identifier') {
      const [first, second] = node.arguments;
      if (node.callee.name === 'field') add(current, literal(first), literal(second));
      if (node.callee.name === 'section' && literal(first)) {
        const table = second?.type === 'Identifier' ? tables.get(second.name) : second;
        const section = open(literal(first), next.component, next.staffOnly);
        for (const row of table?.elements ?? []) {
          if (row?.type === 'ArrayExpression') add(section, literal(row.elements[0]), literal(row.elements[1]));
        }
      }
    }

    // Children in source order. The parser's keys are alphabetical, which
    // visits a ternary's `alternate` before its `consequent`.
    const children = [];
    for (const key of Object.keys(node)) {
      if (key === 'parent' || key === 'loc' || key === 'range') continue;
      const value = node[key];
      for (const child of Array.isArray(value) ? value : [value]) {
        if (child && typeof child.type === 'string') children.push(child);
      }
    }
    children.sort((a, b) => a.range[0] - b.range[0]);
    for (const child of children) walk(child, next);
  };

  walk(ast, { component: null, staffOnly: false });
  // `section()` declares its Section inside the helper, with a variable title.
  // That JSX opened nothing (no literal title), so every real section is here.
  return sections.filter((s) => s.fields.length > 0);
}

export function extractAll() {
  const layout = {};
  for (const file of TAB_FILES) {
    layout[basename(file, '.tsx')] = extractTabLayout(readFileSync(join(TAB_DIR, file), 'utf8'));
  }
  return layout;
}

export function render(layout) {
  return [
    '// Generated by `node scripts/tab-layout.mjs` from the patient tabs. Do not edit.',
    '// tabLayout.test.ts fails when this is stale: regenerate after changing a tab.',
    "import type { TabLayout } from './tabLayoutTypes';",
    '',
    `export const TAB_LAYOUT: TabLayout = ${JSON.stringify(layout, null, 2)};`,
    '',
  ].join('\n');
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  writeFileSync(OUTPUT, render(extractAll()));
  console.log(`wrote ${OUTPUT}`);
}
