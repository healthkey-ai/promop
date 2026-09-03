/**
 * No field may render an editable box on two tabs.
 *
 * Four of the Blood tab's five sections once rendered the same field keys as
 * sections already on Labs — fifteen analytes with two boxes each (#955), and
 * `beta2_microglobulin` was on Labs and Disease. Asserting the absence of four
 * section *titles* does not hold that line: re-adding `psa_ng_ml` under a
 * section named anything else passes.
 *
 * DiseaseTab is scanned but compared only against the other tabs, never against
 * itself: its disease sections are mutually exclusive (`switch (diseaseType)`),
 * so `stage` legitimately appears in three of them. GeneralTab renders `region`
 * twice as the two arms of a ternary, for the same reason.
 *
 * General×Disease is a known, pre-existing overlap (#960) and is excluded until
 * that is settled — a failing test nobody can act on gets deleted.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';

// Relative to this file, not the cwd: `npx vitest --root frontend` and a run
// launched from the repo root both resolve, where process.cwd() would ENOENT
// and turn the only cross-tab guard into an unrelated hard failure.
//
// import.meta.url is not guaranteed to be a file: URL under the test
// transform, so fall back rather than throw on the scheme.
const HERE = (() => {
  const url = import.meta.url;
  const dir = url.slice(0, url.lastIndexOf('/') + 1);
  return dir.startsWith('file:') ? fileURLToPath(dir) : dir.replace(/^[a-z]+:\/\//, '/');
})();

function fieldKeys(file: string): Set<string> {
  const src = readFileSync(HERE + file, 'utf8');
  return new Set([
    ...src.matchAll(/\[\s*'[^']*'\s*,\s*'([a-z0-9_]+)'\s*\]/g),      // [label, key]
    ...src.matchAll(/<ClinicalField[\s\S]*?\bname="([a-z0-9_]+)"/g),    // inline JSX
    ...src.matchAll(/field\('[^']*',\s*'([a-z0-9_]+)'/g),               // field(label, key)
  ].map((m) => m[1]));
}

/** Exact, so a regex that silently stops matching fails loudly instead of
 *  degrading the overlap check below into a no-op. */
const EXPECTED_COUNTS: Record<string, number> = {
  'BloodTab.tsx': 8,
  'LabsTab.tsx': 32,
};

// Pre-existing and tracked in #960; everything else must stay disjoint.
const KNOWN_OVERLAPS = new Set(['GeneralTab.tsx|DiseaseTab.tsx']);

describe('tab field ownership', () => {
  it('counts the fields it thinks it is scanning', () => {
    for (const [file, expected] of Object.entries(EXPECTED_COUNTS)) {
      expect(`${file}=${fieldKeys(file).size}`).toBe(`${file}=${expected}`);
    }
  });

  it('renders no field on two different tabs', () => {
    const files = readdirSync(HERE).filter((f) => f.endsWith('Tab.tsx'));
    const keys = new Map(files.map((f) => [f, fieldKeys(f)]));
    const clashes: string[] = [];

    for (let i = 0; i < files.length; i += 1) {
      for (let j = i + 1; j < files.length; j += 1) {
        const [a, b] = [files[i], files[j]];
        if (KNOWN_OVERLAPS.has(`${a}|${b}`) || KNOWN_OVERLAPS.has(`${b}|${a}`)) continue;
        for (const key of keys.get(a)!) {
          if (keys.get(b)!.has(key)) clashes.push(`${key}: ${a} and ${b}`);
        }
      }
    }
    expect(clashes).toEqual([]);
  });
});
