/**
 * No PatientInfo field may render an editable box on two tabs.
 *
 * That was the bug in #955: fifteen analytes each had a box on Blood *and*
 * Labs, and `beta2_microglobulin` had one on Labs and Disease. One value, two
 * inputs, nothing on screen saying they are the same field — whichever the user
 * edits last wins.
 *
 * This replaces a vitest guard that regex-parsed the TSX. That guard was wrong
 * in five distinct ways across as many review rounds, and every version passed
 * its own tests: `[^>]*?` could not cross the `>` in an arrow-function prop, so
 * a real duplicate went unseen; a table written `[string, string][]` rather than
 * `Array<[string, string]>` was invisible; a pinned field count only moved when
 * an already-matched field stopped matching, never when a new one was never
 * matched at all. Reading the AST removes that whole failure class — there is no
 * pattern left to get subtly wrong, because the parser has already done the work.
 *
 * Cross-file state: ESLint lints a run in one process, so the map below
 * accumulates across files and a clash is reported on the second tab linted.
 * It is keyed by file and overwritten each pass, so re-linting one file in watch
 * mode cannot make it clash with its own previous self. The limitation worth
 * knowing: linting a single tab in isolation sees no other tab, so only a full
 * `eslint .` — which is what `npm run lint` and CI run — enforces this.
 */

/** file -> the field keys it renders. Module scope: one entry per lint run. */
const fieldsByFile = new Map();

/**
 * Fields that legitimately render on two tabs today, keyed per field so the
 * exemption cannot widen. Exempting the whole tab *pair* would permanently
 * excuse the two largest tabs, and a new duplicate between them would pass.
 *
 * These three are #960. Once that lands the rule reports them as stale, so a
 * fixed duplicate cannot leave a dead exemption behind it.
 */
const KNOWN_DUPLICATES = new Map([
  ['disease', ['DiseaseTab.tsx', 'GeneralTab.tsx']],
  ['histologic_type', ['DiseaseTab.tsx', 'GeneralTab.tsx']],
  ['stage', ['DiseaseTab.tsx', 'GeneralTab.tsx']],
]);

const FIELD_KEY = /^[a-z][a-z0-9_]*$/;

/**
 * Drop what previous files contributed. Only the rule's own tests need this:
 * within one `eslint .` the accumulation across files is the whole mechanism.
 */
export const __resetTabFieldState = () => fieldsByFile.clear();

const known = (field, a, b) => {
  const pair = KNOWN_DUPLICATES.get(field);
  return Boolean(pair && pair.includes(a) && pair.includes(b));
};

export default {
  meta: {
    type: 'problem',
    docs: {
      description:
        'Disallow rendering the same PatientInfo field on more than one tab.',
    },
    schema: [],
    messages: {
      duplicate:
        "Field '{{field}}' renders an editable box on both {{other}} and this "
        + 'tab. One value with two inputs means whichever the user edits last '
        + 'wins. Keep the tab where the field means something and delete the '
        + 'other — which may well be {{other}}, not this file: ESLint can only '
        + 'report a cross-file clash on whichever half it reaches second.',
      unreadable:
        'Cannot tell which field <ClinicalField name={{{expr}}}> renders, so '
        + 'this rule cannot check it for duplicates. Give it a literal name, or '
        + "declare it in a field table the tab's section() renders.",
      stale:
        "'{{field}}' is listed as a known duplicate of {{other}}, but it no "
        + 'longer renders on both. Delete the KNOWN_DUPLICATES entry in '
        + 'eslint-rules/no-duplicate-tab-field.js so the exemption does not '
        + 'outlive the duplicate.',
    },
  },

  create(context) {
    const filename = context.filename ?? context.getFilename();
    const base = filename.split('/').pop();
    if (!base.endsWith('Tab.tsx')) return {};

    /** field key -> the node to blame if it proves to be a duplicate. */
    const found = new Map();
    /** const name -> ArrayExpression, for tables a section() may render. */
    const tables = new Map();
    /** const names actually handed to section(). */
    const rendered = new Set();

    const addKey = (key, node) => {
      if (FIELD_KEY.test(key) && !found.has(key)) found.set(key, node);
    };

    /** Read the `['Label', 'field_key']` rows of an array literal. */
    const readTable = (array) => {
      for (const row of array.elements) {
        if (row?.type !== 'ArrayExpression' || row.elements.length < 2) continue;
        const key = row.elements[1];
        if (key?.type === 'Literal' && typeof key.value === 'string') addKey(key.value, key);
      }
    };

    return {
      // <ClinicalField name="field_key" …>
      JSXOpeningElement(node) {
        if (node.name.type !== 'JSXIdentifier' || node.name.name !== 'ClinicalField') return;
        const attr = node.attributes.find(
          (a) => a.type === 'JSXAttribute' && a.name.name === 'name',
        );
        if (!attr?.value) return;

        if (attr.value.type === 'Literal' && typeof attr.value.value === 'string') {
          addKey(attr.value.value, attr);
          return;
        }
        if (attr.value.type === 'JSXExpressionContainer') {
          const expr = attr.value.expression;
          // `name={name}` is the helper's loop variable: its keys come from the
          // field table the helper iterates, which this rule reads separately.
          if (expr.type === 'Identifier' && expr.name === 'name') return;
          // Anything else is invisible to this rule, and silence there is what
          // let a duplicate through review. Report rather than skip.
          context.report({
            node: attr,
            messageId: 'unreadable',
            data: { expr: context.sourceCode.getText(expr) },
          });
        }
      },

      // const COUNTS = [['Label', 'field_key'], …]
      VariableDeclarator(node) {
        if (node.id.type === 'Identifier' && node.init?.type === 'ArrayExpression') {
          tables.set(node.id.name, node.init);
        }
      },

      CallExpression(node) {
        if (node.callee.type !== 'Identifier') return;
        // What makes an array a *field* table is that a section renders it. An
        // options list like [['Yes','yes'], ['No','no']] is declared
        // identically, and two tabs sharing one is not a field duplicate.
        if (node.callee.name === 'section' && node.arguments[1]?.type === 'Identifier') {
          rendered.add(node.arguments[1].name);
        }
        // field('Label', 'field_key', type)
        if (node.callee.name === 'field') {
          const key = node.arguments[1];
          if (key?.type === 'Literal' && typeof key.value === 'string') addKey(key.value, key);
        }
      },

      'Program:exit'(program) {
        for (const name of rendered) {
          const table = tables.get(name);
          if (table) readTable(table);
        }

        fieldsByFile.set(base, new Set(found.keys()));

        for (const [otherFile, otherKeys] of fieldsByFile) {
          if (otherFile === base) continue;
          for (const [key, node] of found) {
            if (!otherKeys.has(key)) continue;
            if (known(key, base, otherFile)) continue;
            context.report({ node, messageId: 'duplicate', data: { field: key, other: otherFile } });
          }
        }

        // A known duplicate that is no longer rendered on both tabs is a dead
        // exemption. Asking "do I still have it?" would miss the case where
        // *this* tab is the one that dropped it and is linted first, so the
        // check is on the pair, once both halves have been seen.
        for (const [field, pair] of KNOWN_DUPLICATES) {
          if (!pair.includes(base)) continue;
          const other = pair.find((f) => f !== base);
          const otherKeys = fieldsByFile.get(other);
          if (!otherKeys) continue; // the other tab has not been linted yet
          if (found.has(field) && otherKeys.has(field)) continue; // still live
          context.report({ node: program, messageId: 'stale', data: { field, other } });
        }
      },
    };
  },
};
