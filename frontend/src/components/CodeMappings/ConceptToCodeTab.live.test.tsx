/**
 * Live round trip for #1915: Concept to Code → Edit Mapping → back.
 *
 * Nothing is mocked: the real ConceptToCodeTab and EditMappingDialog, the real
 * axios instance, real HTTP to a Django server on a real Postgres database. It
 * proves the example-patient lookup returns what the dialog renders, and that a
 * save from the dialog lands the code on the concept the curator started from.
 *
 * Skipped unless CONCEPT_TO_CODE_LIVE_URL is set, so CI is unaffected. It
 * needs a database holding a standard concept with a patient field, a proposed
 * source code with no destination, and two patients whose source inventory
 * carries that code with different metadata. The save consumes the fixture
 * (the code gains a destination), so reseed between runs.
 *
 * As in CodeMappingPage.live.test.tsx, the component's axios uses the '/api'
 * prefix, so requests go through the Vite dev proxy (port 3000 → 9200):
 *
 *   DATABASE_URL=postgresql://postgres@localhost:5433/promop_dev DEBUG=True \
 *     .venv/bin/python manage.py runserver 9200 --noreload
 *   npm --prefix frontend run dev
 *   CONCEPT_TO_CODE_LIVE_URL=http://localhost:9200/api \
 *     CONCEPT_TO_CODE_LIVE_TOKEN=<bearer> \
 *     CONCEPT_TO_CODE_LIVE_CONCEPT="Albumin" CONCEPT_TO_CODE_LIVE_CONCEPT_ID=3024561 \
 *     CONCEPT_TO_CODE_LIVE_SOURCE=SNOMED:RT1915 \
 *     npm test -- --run src/components/CodeMappings/ConceptToCodeTab.live.test.tsx
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeAll, describe, expect, it } from 'vitest';
import api from '@/api/axios';
import ConceptToCodeTab from './ConceptToCodeTab';

const LIVE_URL = process.env.CONCEPT_TO_CODE_LIVE_URL;
const LIVE_TOKEN = process.env.CONCEPT_TO_CODE_LIVE_TOKEN;
const CONCEPT = process.env.CONCEPT_TO_CODE_LIVE_CONCEPT || 'Albumin';
const CONCEPT_ID = Number(process.env.CONCEPT_TO_CODE_LIVE_CONCEPT_ID || 3024561);
const SOURCE = process.env.CONCEPT_TO_CODE_LIVE_SOURCE || 'SNOMED:RT1915';

const describeLive = LIVE_URL && LIVE_TOKEN ? describe : describe.skip;

describeLive('ConceptToCodeTab against a live backend', () => {
  // The browser authenticates with a session cookie (#1216); the API also
  // accepts a bearer token, which is what a test outside the browser has.
  beforeAll(() => { api.defaults.headers.common.Authorization = `Bearer ${LIVE_TOKEN}`; });

  it('edits a source code with patient Source Metadata and lands it on the concept', async () => {
    render(<ConceptToCodeTab canApprove />);
    fireEvent.change(await screen.findByRole('textbox', { name: 'Search standard concepts' }), { target: { value: CONCEPT } });
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(CONCEPT) }, { timeout: 10000 }));
    const coverage = await screen.findByRole('dialog', { name: new RegExp(`Source codes for .*${CONCEPT}`) });

    // The code has no destination yet, so it is suggested, not linked: a
    // curator-review run retrieves it without spending a ranker call (#2010).
    fireEvent.change(within(coverage).getByLabelText('Source suggestion ranker'), { target: { value: 'none' } });
    fireEvent.change(within(coverage).getByLabelText('Maximum source candidates'), { target: { value: '100' } });
    fireEvent.click(within(coverage).getByRole('button', { name: 'Suggest source codes' }));
    fireEvent.click(await within(coverage).findByRole('button', { name: SOURCE }, { timeout: 30000 }));

    const editor = await screen.findByRole('dialog', { name: 'Edit Mapping' }, { timeout: 10000 });
    await waitFor(() => expect(within(editor).getByLabelText('Destination Concept ID')).toHaveValue(CONCEPT_ID), { timeout: 10000 });

    // Example patients come from the server, most-seen first, with their own metadata.
    const picker = await within(editor).findByRole('combobox', { name: 'Example patient' }, { timeout: 10000 });
    const options = within(picker).getAllByRole('option');
    expect(options.length).toBe(2);
    expect(options[0].textContent).toMatch(/Seen 4$/);
    expect(within(editor).getByText(/"specimen": "serum"/)).toBeInTheDocument();
    fireEvent.change(picker, { target: { value: (options[1] as HTMLOptionElement).value } });
    expect(await within(editor).findByText(/"specimen": "plasma"/)).toBeInTheDocument();

    fireEvent.click(within(editor).getByRole('button', { name: 'Update Mapping' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Edit Mapping' })).not.toBeInTheDocument(), { timeout: 15000 });

    // Read it back: the code now maps to the concept, under Existing mappings.
    fireEvent.click(within(coverage).getByRole('button', { name: 'Existing mappings' }));
    const linked = await within(coverage).findByRole('button', { name: SOURCE }, { timeout: 10000 });
    const row = linked.closest('tr')!;
    expect(within(row).getByText(`OMOP ${CONCEPT_ID} · Standard`)).toBeInTheDocument();
  }, 60000);
});
