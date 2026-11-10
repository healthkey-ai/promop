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

import { mountFields } from './widget.patient-info';

const disposers: Array<() => void> = [];
afterEach(async () => {
  await act(async () => { disposers.splice(0).forEach((dispose) => dispose()); });
  document.body.innerHTML = '';
});

it('renews the store from a slot Retry after the patient changed', async () => {
  const context = { apiBase: '/api', token: 'mount-fields-identity', readOnly: false };
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

  person = 2;
  await act(async () => { await original.load(); });
  fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));

  expect(await screen.findByDisplayValue('182')).toBeInTheDocument();
  expect(runtime.store).not.toBe(original);
  expect(original.getSnapshot().personId).toBe(1);
});
