/**
 * Every field the server calls writable, written through the app's own path and
 * read back after derivation.
 *
 * The descriptor is a claim: "this field can be written, here is the fact to
 * write". Each tab renders from that claim, so a wrong entry produces a box that
 * accepts input and loses it. Individual round trips have been checked as each
 * tab was converted; this checks all of them at once, which is the only way to
 * know the claim holds field by field rather than in the cases someone thought
 * to try.
 *
 * Uses a throwaway patient so nothing real is touched and cleanup is a delete.
 */
import { describe, it, expect, beforeAll } from 'vitest';
import { fetchWritableFields, __resetWritableFieldsCache,
         type FieldDescriptor } from '@/hooks/useWritableFields';
import { writeFieldValue, writeFieldValues } from '@/api/clinicalFacts';
import api from '@/api/axios';

const PERSON = 990001;

beforeAll(() => {
  document.cookie = 'sessionid=svlpo0jc6pm4ey52ih43ruykpv2cio51';
  document.cookie = 'csrftoken=aTaONYK8cVsQ9ZR0dOt8QlfacFA4iomG';
  __resetWritableFieldsCache();
});

const record = async () =>
  (await api.get(`/patient-info/${PERSON}/`)).data.patient_info as Record<string, unknown>;

/** A value the field should accept, by the kind the descriptor declares. */
function probeValue(field: string, descriptor: FieldDescriptor, seed: number): unknown {
  switch (descriptor.value_kind) {
    case 'number': return 40 + (seed % 30);
    case 'date': return '2025-04-15';
    case 'boolean': return true;
    default:
      // A curated set means only these values can be coded, so pick from it
      // rather than inventing a string the server would drop.
      if (descriptor.options?.length) return descriptor.options[0].value;
      // Some string columns enforce a real format. Sending a generic probe to an
      // email or to a 2-character state code tests the probe, not the field.
      if (field === 'email') return `probe${seed}@example.org`;
      if (field === 'region') return 'MA';
      return `probe-${seed}`;
  }
}

/** Did the value come back? Compared loosely: the projection casts. */
function came_back(written: unknown, read: unknown): boolean {
  if (read === null || read === undefined) return false;
  if (typeof written === 'number') return Number(read) === written;
  if (typeof written === 'boolean') {
    return read === written || String(read).toLowerCase() === String(written);
  }
  const w = String(written).toLowerCase();
  const r = String(read).toLowerCase();
  return r === w || r.includes(w) || (Array.isArray(read) && read.includes(written));
}

describe('every writable field round trips', () => {
  it('writes each one and reads it back', async () => {
    const descriptors = await fetchWritableFields(PERSON);
    const writable = Object.entries(descriptors)
      .filter(([, e]) => e.writable)
      .sort(([a], [b]) => a.localeCompare(b));

    expect(writable.length).toBeGreaterThan(70);

    const failures: Array<{ field: string; target?: string; wrote: unknown; read: unknown; error?: string }> = [];
    const passed: string[] = [];

    let seed = 0;
    for (const [field, descriptor] of writable) {
      if (field === 'suppress_demographics_for_others') {
        // Setting this redacts date of birth, name and the whole address from
        // every reader who is not the account holder — including this one. It
        // would blind the sweep to its own later writes, so it is exercised in
        // its own test rather than in the middle of this one.
        continue;
      }
      // The API throttles, and this fires several requests per field. A real
      // user does not type 81 fields in ten seconds, so pacing here measures the
      // round trip rather than the rate limiter.
      await new Promise((r) => setTimeout(r, 120));
      const value = probeValue(field, descriptor, seed++);
      try {
        if (field === 'latitude' || field === 'longitude') {
          // Both or neither: the record carries a check constraint saying so, and
          // the endpoint refuses one alone. Sent as a batch, which is exactly
          // how the editor sends them — one request, so they arrive together.
          await writeFieldValues(PERSON, [
            { field: 'latitude', descriptor: descriptors.latitude, value: 42.36 },
            { field: 'longitude', descriptor: descriptors.longitude, value: -71.06 },
          ]);
        } else {
          await writeFieldValue(PERSON, field, descriptor, value);
        }
      } catch (err) {
        const detail = (err as { response?: { data?: unknown } })?.response?.data;
        failures.push({
          field, target: descriptor.target, wrote: value, read: undefined,
          error: `write rejected: ${JSON.stringify(detail ?? String(err)).slice(0, 160)}`,
        });
        continue;
      }
      const after = await record();
      const expected = field === 'latitude' ? 42.36
        : field === 'longitude' ? -71.06 : value;
      if (came_back(expected, after[field])) passed.push(field);
      else failures.push({ field, target: descriptor.target, wrote: value, read: after[field] });
    }

    // Printed rather than only asserted: the useful output is which fields fail
    // and how, not that some number of them did.
    console.log(`\nPASS ${passed.length} / ${writable.length}`);
    if (failures.length) {
      console.log('\nFAILURES');
      for (const f of failures) {
        console.log(`  ${f.field}  [${f.target}]  wrote=${JSON.stringify(f.wrote)}  ` +
                    `read=${JSON.stringify(f.read)}${f.error ? '  ' + f.error : ''}`);
      }
    }
    expect({ failed: failures.length, of: writable.length }).toEqual(
      { failed: 0, of: writable.length },
    );
  }, 600000);
});
