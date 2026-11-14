/** @vitest-environment jsdom */
/**
 * mountFields on a runtime whose client starts answering for another patient:
 * the slot's Retry renews the store instead of reloading the stuck one, and
 * the runtime's retirement then watches the fresh store.
 */
import { act, fireEvent, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { patientFormRuntime } from './patientFormRuntime';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('./injectStyles', () => ({ injectStyles: () => {} }));

import { mountFields, mountTherapyLines } from './widget.patient-info';

const disposers: Array<() => void> = [];
afterEach(async () => {
  await act(async () => { disposers.splice(0).forEach((dispose) => dispose()); });
  document.body.innerHTML = '';
});

it('renews the store from a slot Retry after the patient changed', async () => {
  const onSaveError = vi.fn();
  const context = { apiBase: '/api', token: 'mount-fields-identity', readOnly: false, onSaveError };
  const runtime = patientFormRuntime(context);
  let person = 1;
  vi.spyOn(runtime.client, 'get').mockImplementation(async (url: string) => ({
    data: url.includes('writable-fields')
      ? { height: { writable: true, target: 'patient_record', control: 'number', label: 'Height' } }
      : { patient_info: { person_id: person, height: person === 1 ? 170 : 182 }, write_enabled: true },
  }));
  const container = document.createElement('div');
  container.setAttribute('data-promop-form', '');
  const root = document.createElement('div');
  const slot = document.createElement('div');
  slot.setAttribute('data-promop-field', 'height');
  container.append(root, slot);
  document.body.appendChild(container);

  await act(async () => { disposers.push(mountFields(root, context)); });
  expect(await screen.findByDisplayValue('170')).toBeInTheDocument();
  const original = runtime.store;

  // An edit is queued when the client starts answering for another patient.
  fireEvent.change(screen.getByDisplayValue('170'), { target: { value: '180' } });
  person = 2;
  await act(async () => { await original.load(); });
  fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));

  expect(await screen.findByDisplayValue('182')).toBeInTheDocument();
  expect(runtime.store).not.toBe(original);
  expect(original.getSnapshot().personId).toBe(1);
  // The retirement's own error publish tells the host once; the renewal must
  // not deliver the same lost draft a second time.
  expect(onSaveError).toHaveBeenCalledTimes(1);
});

it('moves a second card on the same runtime when one slot renews', async () => {
  const context = { apiBase: '/api', token: 'mount-fields-fan-out', readOnly: false };
  const runtime = patientFormRuntime(context);
  let person = 1;
  vi.spyOn(runtime.client, 'get').mockImplementation(async (url: string) => ({
    data: url.includes('writable-fields')
      ? { height: { writable: true, target: 'patient_record', control: 'number', label: 'Height' } }
      : { patient_info: { person_id: person, height: person === 1 ? 170 : 182 }, write_enabled: true },
  }));
  // Two host cards, two React roots, one connection.
  const cards = [0, 1].map(() => {
    const container = document.createElement('div');
    container.setAttribute('data-promop-form', '');
    const root = document.createElement('div');
    const slot = document.createElement('div');
    slot.setAttribute('data-promop-field', 'height');
    container.append(root, slot);
    document.body.appendChild(container);
    return { root, slot };
  });

  for (const card of cards) {
    await act(async () => { disposers.push(mountFields(card.root, context)); });
  }
  expect(await screen.findAllByDisplayValue('170')).toHaveLength(2);

  person = 2;
  await act(async () => { await runtime.store.load(); });
  const retries = await screen.findAllByRole('button', { name: 'Retry' });
  expect(retries).toHaveLength(2);
  fireEvent.click(retries[0]);

  // The other card leaves the retired store without a Retry of its own.
  expect(await screen.findAllByDisplayValue('182')).toHaveLength(2);
  expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
});

it('moves a mounted therapy editor when a mountFields card renews', async () => {
  const context = { apiBase: '/api', token: 'mount-fields-therapy', readOnly: false };
  const runtime = patientFormRuntime(context);
  let person = 1;
  vi.spyOn(runtime.client, 'get').mockImplementation(async (url: string) => ({
    data: url.includes('writable-fields')
      ? { height: { writable: true, target: 'patient_record', control: 'number', label: 'Height' } }
      : { patient_info: { person_id: person, height: person === 1 ? 170 : 182,
        lines_of_therapy: [{ line: 1, episode_id: person, regimen: person === 1 ? 'VRd' : 'KRd',
          start_date: '2024-01-01' }] }, write_enabled: true },
  }));
  const container = document.createElement('div');
  container.setAttribute('data-promop-form', '');
  const fieldsRoot = document.createElement('div');
  const slot = document.createElement('div');
  slot.setAttribute('data-promop-field', 'height');
  container.append(fieldsRoot, slot);
  const therapyRoot = document.createElement('div');
  document.body.append(container, therapyRoot);

  await act(async () => {
    disposers.push(mountFields(fieldsRoot, context));
    disposers.push(mountTherapyLines(therapyRoot, context));
  });
  expect(await screen.findByDisplayValue('170')).toBeInTheDocument();
  expect(await screen.findByText(/VRd/)).toBeInTheDocument();

  person = 2;
  await act(async () => { await runtime.store.load(); });
  // Retry on the field card's slot, not on the therapy editor.
  const fieldRetry = [...slot.querySelectorAll('button')].find((b) => b.textContent === 'Retry');
  expect(fieldRetry).toBeTruthy();
  fireEvent.click(fieldRetry!);

  expect(await screen.findByDisplayValue('182')).toBeInTheDocument();
  // The therapy editor read the store once at mount before; it must follow.
  expect(await screen.findByText(/KRd/)).toBeInTheDocument();
  expect(therapyRoot.querySelector('[role="alert"]')).toBeNull();
});
