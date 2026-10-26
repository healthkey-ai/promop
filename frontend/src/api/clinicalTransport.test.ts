import { describe, expect, it } from 'vitest';
import type { AxiosInstance } from 'axios';

import { clinicalClient, pushClinicalTransport, resetClinicalTransport } from './clinicalTransport';

const client = (name: string) => ({ name } as unknown as AxiosInstance);

describe('clinical transport ownership', () => {
  it('restores the previous owner when a later one is released', () => {
    resetClinicalTransport();
    const page = client('page');
    const widget = client('widget');

    const releasePage = pushClinicalTransport(page, '/page');
    const releaseWidget = pushClinicalTransport(widget, '/widget');
    expect(clinicalClient()).toBe(widget);

    releaseWidget();
    expect(clinicalClient()).toBe(page);

    releasePage();
    expect(clinicalClient()).not.toBe(page);
  });

  it('pops an owner released out of order without disturbing the top', () => {
    resetClinicalTransport();
    const first = client('first');
    const second = client('second');

    const releaseFirst = pushClinicalTransport(first, '/first');
    const releaseSecond = pushClinicalTransport(second, '/second');

    // The first widget unmounts while the second is still open (two dialogs, a
    // navigation) — the second must keep its own transport.
    releaseFirst();
    expect(clinicalClient()).toBe(second);

    releaseSecond();
    expect(clinicalClient()).not.toBe(second);
  });
});
