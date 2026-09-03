/**
 * No field may render an editable box on two tabs.
 *
 * That was the bug in #955: fifteen analytes each had a box on Blood *and*
 * Labs, and `beta2_microglobulin` had one on Labs and Disease. Asserting the
 * absence of a few section *titles* does not hold that line — re-adding a field
 * under a section named anything else passes.
 *
 * The field lists are module-private consts, so this reads the sources. Regexes
 * over TSX are fragile, and a guard that silently stops seeing a field is worse
 * than no guard: it reports green over the bug it was written for. So every
 * scan here is paired with a **completeness check** that fails when the scan
 * cannot read something it should have.
 *
 * That pairing is the point. An earlier version matched names inside
 * `<ClinicalField …>` with `[^>]*?`, which cannot cross the `>` in a prop like
 * `onChange={(f, v) => onChange(f, v)}`. Adding such an element with a
 * duplicate `name` left both tests green, because a pinned field *count* only
 * moves when an already-matched field stops matching — a brand-new invisible
 * field does not move it at all.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';

/**
 * This directory, resolved from the module URL rather than the cwd so that
 * `npx vitest --root frontend` and a run launched from the repo root both work.
 *
 * Under jsdom the module URL is `http:`, not `file:`, so its *pathname* is the
 * project-relative path and is joined to the cwd. An earlier version mangled
 * the scheme with a regex and produced a garbage path that threw ENOENT — an
 * unrelated hard failure wearing this test's name. Nothing is guessed here: if
 * this resolves wrongly, the file-list assertion below fails and says so.
 */
const HERE = (() => {
  const url = new URL('.', import.meta.url);
  if (url.protocol === 'file:') return fileURLToPath(url);
  return join(process.cwd(), url.pathname);
})();

interface Scan {
  keys: Set<string>;
  /** Elements or tables the scan could not read. Non-empty means it is lying. */
  unreadable: string[];
}

function scan(file: string): Scan {
  const src = readFileSync(join(HERE, file), 'utf8');
  const keys = new Set<string>();
  const unreadable: string[] = [];

  // 1. Inline `<ClinicalField … name="field_key" …>` (DiseaseTab, GeneralTab).
  //    Split on the opening tag and take each element's own text, so a `>`
  //    inside an arrow-function prop cannot end the element early.
  const elements = src.split(/<ClinicalField\b/).slice(1);
  for (const element of elements) {
    const literal = /\bname="([a-z0-9_]+)"/.exec(element);
    if (literal) {
      keys.add(literal[1]);
      continue;
    }
    // `name={name}` is the one legitimate computed form: the helper that
    // renders it iterates a field table, and the table scan below reads those
    // keys. Any other computed name is invisible to this guard, so it fails
    // here rather than being skipped in silence — that silence is what let a
    // duplicate through in review.
    if (/\bname=\{name\}/.test(element)) continue;
    const shown = /\bname=\{([^}]*)\}/.exec(element);
    unreadable.push(
      `<ClinicalField name={${shown ? shown[1] : '?'}}> in ${file}: this guard `
      + 'cannot read a computed field name. Add it to the tab\'s field table, '
      + 'or teach this scan about it.',
    );
  }

  // 2. `[label, key]` tables, but only those a section actually renders — an
  //    options list like [['Yes','yes'], ['No','no']] is declared identically
  //    to a field table, and flagging one shared by two tabs would fail this
  //    suite over work with nothing to do with field ownership.
  const rendered = [...src.matchAll(/section\(\s*'[^']*'\s*,\s*([A-Z_][A-Z0-9_]*)/g)]
    .map((m) => m[1]);
  for (const name of new Set(rendered)) {
    // Annotation-agnostic: `Array<[string, string]>` and `[string, string][]`
    // are the same declaration, and requiring one spelling let a table written
    // the other way go unseen.
    const table = new RegExp(`const ${name}\\b[^=]*=\\s*\\[([\\s\\S]*?)\\n\\];`).exec(src);
    if (!table) {
      unreadable.push(`section() renders ${name}, which this scan cannot parse, in ${file}`);
      continue;
    }
    for (const row of table[1].matchAll(/\[\s*'[^']*'\s*,\s*'([a-z0-9_]+)'\s*\]/g)) {
      keys.add(row[1]);
    }
  }

  // 3. `field('Label', 'field_key', type)` (DiseaseTab's staging section).
  for (const m of src.matchAll(/\bfield\('[^']*',\s*'([a-z0-9_]+)'/g)) keys.add(m[1]);

  return { keys, unreadable };
}

/**
 * Exact, for every file the overlap check scans. A regex that stops matching an
 * existing field reduces the count and fails here rather than degrading the
 * check below into a green no-op. It does NOT catch a newly added field the
 * scan cannot see — that is what `unreadable` is for.
 *
 * Adding a field to a tab is routine (CLAUDE.md's "Adding a New Patient
 * Attribute" requires it), so this failing is normal: update the number.
 */
const EXPECTED_COUNTS: Record<string, number> = {
  'BehaviorTab.tsx': 27,
  'BloodTab.tsx': 8,
  'DiseaseTab.tsx': 86,
  'GeneralTab.tsx': 45,
  'LabsTab.tsx': 32,
  'TreatmentTab.tsx': 26,
  'WearableTab.tsx': 20,
};

/**
 * Known duplicates, per key rather than per tab pair.
 *
 * Excluding the whole GeneralTab×DiseaseTab pair would permanently exempt the
 * two largest tabs — a *new* duplicate between them would pass green, and
 * nothing would prompt removing the exemption once #960 lands. Keyed entries
 * fail as soon as the known three are fixed, which is the reminder.
 */
const KNOWN_DUPLICATES = new Set([
  'disease: DiseaseTab.tsx and GeneralTab.tsx',
  'histologic_type: DiseaseTab.tsx and GeneralTab.tsx',
  'stage: DiseaseTab.tsx and GeneralTab.tsx',
]);

const tabFiles = () => readdirSync(HERE).filter((f) => f.endsWith('Tab.tsx')).sort();

describe('tab field ownership', () => {
  it('can read every field declaration on every tab', () => {
    // Without this, an element the regexes cannot parse is indistinguishable
    // from a tab that has no such element — and the overlap check below reports
    // green over a duplicate it never saw.
    const problems = tabFiles().flatMap((f) => scan(f).unreadable);
    expect(problems).toEqual([]);
  });

  it('counts the fields it thinks it is scanning', () => {
    expect(tabFiles()).toEqual(Object.keys(EXPECTED_COUNTS).sort());
    for (const [file, expected] of Object.entries(EXPECTED_COUNTS)) {
      expect(`EXPECTED_COUNTS[${file}]=${scan(file).keys.size}`)
        .toBe(`EXPECTED_COUNTS[${file}]=${expected}`);
    }
  });

  it('renders no field on two different tabs', () => {
    const files = tabFiles();
    const keys = new Map(files.map((f) => [f, scan(f).keys]));
    const clashes: string[] = [];

    for (let i = 0; i < files.length; i += 1) {
      for (let j = i + 1; j < files.length; j += 1) {
        for (const key of keys.get(files[i])!) {
          if (!keys.get(files[j])!.has(key)) continue;
          const clash = `${key}: ${files[i]} and ${files[j]}`;
          if (!KNOWN_DUPLICATES.has(clash)) clashes.push(clash);
        }
      }
    }
    expect(clashes).toEqual([]);
  });

  it('has no stale entry in KNOWN_DUPLICATES', () => {
    // A fixed duplicate must not leave a permanent hole behind it.
    const files = tabFiles();
    const keys = new Map(files.map((f) => [f, scan(f).keys]));
    const live = new Set<string>();
    for (let i = 0; i < files.length; i += 1) {
      for (let j = i + 1; j < files.length; j += 1) {
        for (const key of keys.get(files[i])!) {
          if (keys.get(files[j])!.has(key)) live.add(`${key}: ${files[i]} and ${files[j]}`);
        }
      }
    }
    expect([...KNOWN_DUPLICATES].filter((k) => !live.has(k))).toEqual([]);
  });
});
