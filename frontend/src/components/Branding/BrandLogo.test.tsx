import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import BrandLogo from './BrandLogo';
import { resetDeployedCommit } from '@/utils/deployedCommit';

const get = vi.fn();
vi.stubGlobal('fetch', (...args: unknown[]) => get(...args));

const ok = (body: unknown) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
const status = (code: number) => Promise.resolve({ ok: false, status: code, json: () => Promise.resolve({}) });

const commit = '8798ec24f4cc16e47f8c0ceec69d38d7e5858b17';

beforeEach(() => { get.mockReset(); resetDeployedCommit(); });
afterEach(() => { vi.unstubAllGlobals(); vi.stubGlobal('fetch', (...args: unknown[]) => get(...args)); });

describe('BrandLogo (#2016)', () => {
  it('shows the deployed commit in tiny type under the logo, the full SHA as a tooltip', async () => {
    get.mockImplementation(() => ok({ commit, short: commit.slice(0, 8) }));
    render(<MemoryRouter><BrandLogo /></MemoryRouter>);
    const marker = await screen.findByTestId('deployed-commit');
    expect(marker).toHaveTextContent('8798ec24');
    expect(marker).toHaveAttribute('title', `Deployed commit ${commit}`);
    expect(get).toHaveBeenCalledWith('/api/v1/version/', expect.objectContaining({ credentials: 'omit' }));
    expect(screen.getByRole('link', { name: 'PRomop home' })).toContainElement(marker);
  });

  it('fetches once for every logo on the page', async () => {
    get.mockImplementation(() => ok({ commit, short: commit.slice(0, 8) }));
    render(<MemoryRouter><BrandLogo /><BrandLogo size="toolbar" /></MemoryRouter>);
    expect(await screen.findAllByTestId('deployed-commit')).toHaveLength(2);
    expect(get).toHaveBeenCalledTimes(1);
  });

  it('shows nothing when the build is unknown or the endpoint is unreachable', async () => {
    get.mockImplementation(() => Promise.reject(new Error('offline')));
    render(<MemoryRouter><BrandLogo /></MemoryRouter>);
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(screen.queryByTestId('deployed-commit')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'PRomop home' })).toBeInTheDocument();
  });

  it('shows nothing for a non-2xx answer', async () => {
    get.mockImplementation(() => status(404));
    render(<MemoryRouter><BrandLogo /></MemoryRouter>);
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(screen.queryByTestId('deployed-commit')).not.toBeInTheDocument();
  });

  it('still renders when the client throws instead of rejecting (#2018)', async () => {
    get.mockImplementation(() => { throw new TypeError('fetch is not a function'); });
    render(<MemoryRouter><BrandLogo /></MemoryRouter>);
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(screen.queryByTestId('deployed-commit')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'PRomop home' })).toBeInTheDocument();
  });
});
