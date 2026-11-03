/**
 * Live round trip for #1953: the estimate under Suggest source codes.
 *
 * Nothing is mocked: the real ConceptToCodeTab, real HTTP to a Django server on
 * a real Postgres database. It proves the page shows the server's estimate, and
 * that proposing the codes a run returned takes them out of the next estimate.
 *
 * Skipped unless CONCEPT_TO_CODE_LIVE_URL and CONCEPT_TO_CODE_LIVE_TOKEN are
 * set. Needs a standard concept and more proposed source codes resembling its
 * name than one run of 25 returns; servers as in ConceptToCodeTab.live.test.tsx.
 * Proposing consumes the fixture, so reseed between runs.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeAll, describe, expect, it } from 'vitest';
import api from '@/api/axios';
import ConceptToCodeTab from './ConceptToCodeTab';

const LIVE_URL = process.env.CONCEPT_TO_CODE_LIVE_URL;
const LIVE_TOKEN = process.env.CONCEPT_TO_CODE_LIVE_TOKEN;
const CONCEPT = process.env.CONCEPT_TO_CODE_LIVE_CONCEPT || 'Glucose level outside reference range';
const CONCEPT_ID = Number(process.env.CONCEPT_TO_CODE_LIVE_CONCEPT_ID || 438557);

const describeLive = LIVE_URL && LIVE_TOKEN ? describe : describe.skip;

describeLive('Suggest source codes estimate against a live backend', () => {
  beforeAll(() => { api.defaults.headers.common.Authorization = `Bearer ${LIVE_TOKEN}`; });

  it('shows the server estimate and drops the codes a curator proposed', async () => {
    const estimate = async () => (await api.get(`/v1/concept-to-code/${CONCEPT_ID}/estimate/`,
      { params: { strategies: 'umls,lexical', include_zero_seen: '0' } })).data;
    const before = await estimate();
    expect(before.total).toBeGreaterThan(25);

    render(<ConceptToCodeTab canApprove />);
    // The concept need not back a patient field.
    fireEvent.change(await screen.findByRole('combobox', { name: 'Concept scope' }), { target: { value: 'all' } });
    fireEvent.change(await screen.findByRole('textbox', { name: 'Search standard concepts' }), { target: { value: CONCEPT } });
    fireEvent.click(await screen.findByRole('button', { name: CONCEPT }, { timeout: 10000 }));
    const coverage = await screen.findByRole('dialog', { name: new RegExp(`Source codes for .*${CONCEPT}`) });
    expect(within(coverage).queryByRole('button', { name: 'Refresh' })).not.toBeInTheDocument();

    const runs = Math.ceil(before.total / 25);
    await waitFor(() => expect(within(coverage).getByTestId('source-estimate'))
      .toHaveTextContent(`About ${before.total.toLocaleString()} candidate source codes`), { timeout: 15000 });
    expect(within(coverage).getByTestId('source-estimate')).toHaveTextContent(`about ${runs} runs of 25`);

    // A curator-review run of 5, all approved for this concept: Approve
    // proposes a vocabulary-only candidate from the preview first (#2010).
    fireEvent.change(within(coverage).getByLabelText('Source suggestion ranker'), { target: { value: 'none' } });
    fireEvent.change(within(coverage).getByLabelText('Maximum source candidates'), { target: { value: '5' } });
    fireEvent.click(within(coverage).getByRole('button', { name: 'Suggest source codes' }));
    await within(coverage).findByText('5 candidates retrieved (limit 5).', {}, { timeout: 30000 });
    fireEvent.click(within(coverage).getByRole('checkbox', { name: 'Select displayed proposed sources' }));
    fireEvent.click(within(coverage).getByRole('button', { name: 'Approve selected (5)' }));
    await within(coverage).findByText('Approved 5 of 5 selected mappings.', {}, { timeout: 30000 });

    // The next estimate, from the server and on the page, no longer counts them.
    await waitFor(() => expect(within(coverage).getByTestId('source-estimate'))
      .toHaveTextContent(`About ${(before.total - 5).toLocaleString()} candidate source codes`), { timeout: 15000 });
    expect((await estimate()).total).toBe(before.total - 5);
  }, 120000);
});
