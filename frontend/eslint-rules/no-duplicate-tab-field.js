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
 * matched at all. Reading the AST removes that whole failure class: the parser
 * has already done the work, so there is no pattern left to mis-write.
 *
 * What it does not remove is the rule's dependence on *names* — <ClinicalField>,
 * and the section()/field() helpers the tabs declare their fields through.
 * Rename one and the rule finds nothing, with nothing unreadable to complain
 * about, and every field on that tab is silently exempt. So the rule never just
 * skips: a computed name, a field table it cannot follow, and a field() key it
 * cannot read are each reported, and a tab it can see no field at all on is
 * reported too. Reporting green over what it could not read is what the guard
 * this replaced did, five times.
 *
 * Cross-file state: ESLint lints a run in one process, so the map below
 * accumulates across files and a clash is reported on the second tab linted.
 * It is keyed by file and overwritten each pass, so re-linting one file in watch
 * mode cannot make it clash with its own previous self. The limitation worth
 * knowing: linting a single tab in isolation sees no other tab, so only a full
 * `eslint .` — which is what `npm run lint` and CI run — enforces this.
 */

import { basename } from 'node:path';

/** file -> the field keys it renders. Module scope: one entry per lint run. */
const fieldsByFile = new Map();

/**
 * Fields that legitimately render on two tabs, keyed per field so an exemption
 * cannot widen. Exempting a whole tab *pair* would permanently excuse both, and
 * a new duplicate between them would pass unseen.
 *
 * Empty, and the intent is that it stays that way. It last held disease,
 * histologic_type and stage; #960 moved stage and histology to the Disease tab
 * and disease to General, and the stale check reported all three the moment it
 * did — which is the check doing its job. Add an entry only with an issue
 * number and the reason the duplicate is deliberate.
 */
let KNOWN_DUPLICATES = new Map();

/**
 * Drop what previous files contributed. Only the rule's own tests need this:
 * within one `eslint .` the accumulation across files is the whole mechanism.
 */
export const __resetTabFieldState = () => fieldsByFile.clear();

/**
 * Install an exemption map. Only the rule's own tests use this: the exemption
 * and stale mechanisms still need covering while the production map is empty,
 * and a test that asserted against whatever happened to be in it would go quiet
 * the moment the last entry was fixed — which is exactly when it was still
 * proving something.
 */
export const __setKnownDuplicates = (entries) => {
  KNOWN_DUPLICATES = new Map(entries);
};

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
      unreadableTable:
        'Cannot read the field table section() renders here ({{expr}}), so the '
        + 'fields in it are not checked for duplicates. Pass the array literal '
        + 'itself, or a const bound directly to one.',
      unreadableRow:
        'Cannot read the field key in this row of a table section() renders '
        + '({{expr}}), so that field is not checked for duplicates. Use a '
        + 'string literal for the key.',
      unreadableFieldCall:
        "Cannot tell which field field(…, {{expr}}, …) renders, so it is not "
        + 'checked for duplicates. Pass a string literal.',
      noFields:
        'This tab declares no field that the rule can see. Either it renders '
        + 'none, or it declares them in a way the rule does not know — a helper '
        + 'renamed from section()/field(), say — in which case every field on '
        + 'it is silently exempt from the duplicate check. Teach the rule the '
        + 'new shape.',
      stale:
        "'{{field}}' is listed as a known duplicate of {{other}}, but it no "
        + 'longer renders on both. Delete the KNOWN_DUPLICATES entry in '
        + 'eslint-rules/no-duplicate-tab-field.js so the exemption does not '
        + 'outlive the duplicate.',
    },
  },

  create(context) {
    const filename = context.filename ?? context.getFilename();
    const base = basename(filename);
    if (!base.endsWith('Tab.tsx')) return {};

    /** field key -> the node to blame if it proves to be a duplicate. */
    const found = new Map();
    /** const name -> ArrayExpression, for tables a section() may render. */
    const tables = new Map();
    /** Every section(title, table) call site, resolvable or not. */
    const sectionCalls = [];

    /** Reports already made about something the rule could not read. */
    let blindSpots = 0;
    const reportBlind = (node, messageId, expr) => {
      blindSpots += 1;
      context.report({ node, messageId, data: { expr: context.sourceCode.getText(expr) } });
    };

    const addKey = (key, node) => {
      // Any non-empty string: in the three positions this rule reads from, the
      // string *is* the field name by construction. Filtering on a snake_case
      // pattern only ever loses fields, silently — and a false positive here is
      // loud and one edit away, where a false negative is the whole bug.
      if (key && !found.has(key)) found.set(key, node);
    };

    /** Read the `['Label', 'field_key']` rows of an array literal. */
    const readTable = (array) => {
      for (const row of array.elements) {
        if (!row) continue; // a hole, `[, x]` — nothing was written there
        const key = row.type === 'ArrayExpression' ? row.elements[1] : null;
        if (key?.type === 'Literal' && typeof key.value === 'string') {
          addKey(key.value, key);
          continue;
        }
        // A row this rule cannot name is a field exempt from the duplicate
        // check, and the tab's other rows keep the noFields check quiet — so
        // saying nothing here would hide a duplicate outright.
        reportBlind(row, 'unreadableRow', row);
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
          reportBlind(attr, 'unreadable', expr);
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
        if (node.callee.name === 'section' && node.arguments.length > 1) {
          // Resolved at Program:exit — a table may be declared below its use.
          sectionCalls.push(node.arguments[1]);
        }
        // field('Label', 'field_key', type)
        if (node.callee.name === 'field' && node.arguments.length > 1) {
          const key = node.arguments[1];
          if (key.type === 'Literal' && typeof key.value === 'string') addKey(key.value, key);
          // Anything else is a field this rule cannot name. Reporting beats
          // skipping: an unseen field is one the duplicate check cannot make.
          else reportBlind(key, 'unreadableFieldCall', key);
        }
      },

      'Program:exit'(program) {
        for (const arg of sectionCalls) {
          if (arg.type === 'ArrayExpression') { readTable(arg); continue; }
          const table = arg.type === 'Identifier' ? tables.get(arg.name) : null;
          if (table) { readTable(table); continue; }
          // A table the rule cannot follow — an alias, an import, a call. Its
          // fields are unchecked, and saying nothing here is precisely how the
          // guard this replaced reported green over a duplicate it never saw.
          reportBlind(arg, 'unreadableTable', arg);
        }

        // A tab with no visible fields is far more likely a scan that has gone
        // blind — a renamed helper — than a tab that renders nothing.
        // Only when nothing more specific was said: a tab whose one table is
        // unreadable has already been told exactly what is wrong with it.
        if (found.size === 0 && blindSpots === 0) {
          context.report({ node: program, messageId: 'noFields' });
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
