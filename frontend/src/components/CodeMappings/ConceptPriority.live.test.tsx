/**
 * Live round trip for #1914: rank concepts in Concept to Code against a real backend.
 *
 * Nothing is mocked: the real ConceptToCodeTab, the real axios instance, real
 * HTTP through the Vite dev proxy (3000 → 9200) to Django on Postgres. It
 * proves the server renumbers a move and that the list comes back in the new
 * order, by typed number and by drag and drop.
 *
 * Skipped unless CONCEPT_PRIORITY_LIVE_URL is set, so CI is unaffected. It
 * needs three standard concepts with patient fields whose names share a search
 * term (CONCEPT_PRIORITY_LIVE_SEARCH, default "RT1914") and sort Alpha, Beta,
 * Gamma, with no concept ranked yet. It leaves Gamma ranked first, Beta second.
 *
 *   DATABASE_URL=postgresql://postgres@localhost:5433/promop_dev DEBUG=True \
 *     .venv/bin/python manage.py runserver 9200 --noreload
 *   npm --prefix frontend run dev
 *   CONCEPT_PRIORITY_LIVE_URL=http://localhost:9200/api CONCEPT_PRIORITY_LIVE_TOKEN=<bearer> \
 *     npm test -- --run src/components/CodeMappings/ConceptPriority.live.test.tsx
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeAll, describe, expect, it } from 'vitest';
import api from '@/api/axios';
import ConceptToCodeTab from './ConceptToCodeTab';

const LIVE_URL = process.env.CONCEPT_PRIORITY_LIVE_URL;
const LIVE_TOKEN = process.env.CONCEPT_PRIORITY_LIVE_TOKEN;
const SEARCH = process.env.CONCEPT_PRIORITY_LIVE_SEARCH || 'RT1914';
const describeLive = LIVE_URL && LIVE_TOKEN ? describe : describe.skip;

const order = () => within(screen.getByRole('table', { name: 'Standard concept coverage' }))
  .getAllByRole('button', { name: new RegExp(`^${SEARCH}`) }).map(button => button.textContent);

describeLive('concept priority against a live backend', () => {
  // The browser uses a session cookie (#1216); a test outside it sends a token.
  beforeAll(() => { api.defaults.headers.common.Authorization = `Bearer ${LIVE_TOKEN}`; });

  it('ranks by typed number and by drag, and reads the new order back', async () => {
    render(<ConceptToCodeTab canApprove />);
    fireEvent.change(await screen.findByRole('textbox', { name: 'Search standard concepts' }), { target: { value: SEARCH } });
    await waitFor(() => expect(order()).toEqual([`${SEARCH} Alpha`, `${SEARCH} Beta`, `${SEARCH} Gamma`]), { timeout: 10000 });

    // Type a priority: Beta goes to the top.
    const beta = screen.getByLabelText(`Priority for ${SEARCH} Beta`);
    fireEvent.change(beta, { target: { value: '1' } });
    fireEvent.keyDown(beta, { key: 'Enter' });
    await waitFor(() => expect(order()).toEqual([`${SEARCH} Beta`, `${SEARCH} Alpha`, `${SEARCH} Gamma`]), { timeout: 10000 });
    expect(screen.getByLabelText(`Priority for ${SEARCH} Beta`)).toHaveValue(1);
    expect(screen.getByLabelText(`Priority for ${SEARCH} Alpha`)).toHaveValue(null);

    // Drag the unranked Gamma onto Beta: Gamma takes position 1, Beta moves to 2.
    const gammaRow = screen.getByLabelText(`Priority for ${SEARCH} Gamma`).closest('tr')!;
    const betaRow = screen.getByLabelText(`Priority for ${SEARCH} Beta`).closest('tr')!;
    fireEvent.dragStart(gammaRow);
    fireEvent.dragOver(betaRow);
    fireEvent.drop(betaRow);
    await waitFor(() => expect(order()).toEqual([`${SEARCH} Gamma`, `${SEARCH} Beta`, `${SEARCH} Alpha`]), { timeout: 10000 });
    expect(screen.getByLabelText(`Priority for ${SEARCH} Gamma`)).toHaveValue(1);
    expect(screen.getByLabelText(`Priority for ${SEARCH} Beta`)).toHaveValue(2);

    // Ranked only shows just those two, from the server.
    fireEvent.click(screen.getByRole('checkbox', { name: /Ranked only/ }));
    await waitFor(() => expect(order()).toEqual([`${SEARCH} Gamma`, `${SEARCH} Beta`]), { timeout: 10000 });
  }, 60000);
});
