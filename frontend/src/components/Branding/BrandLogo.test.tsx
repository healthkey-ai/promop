import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import BrandLogo from './BrandLogo';
import { resetDeployedCommit } from '@/utils/deployedCommit';

const get = vi.fn();
vi.mock('@/api/axios', () => ({ default: { get: (...args: unknown[]) => get(...args) } }));

const commit = '8798ec24f4cc16e47f8c0ceec69d38d7e5858b17';

beforeEach(() => { get.mockReset(); resetDeployedCommit(); });

describe('BrandLogo (#2016)', () => {
  it('shows the deployed commit in tiny type under the logo, the full SHA as a tooltip', async () => {
    get.mockResolvedValue({ data: { commit, short: commit.slice(0, 8) } });
    render(<MemoryRouter><BrandLogo /></MemoryRouter>);
    const marker = await screen.findByTestId('deployed-commit');
    expect(marker).toHaveTextContent('8798ec24');
    expect(marker).toHaveAttribute('title', `Deployed commit ${commit}`);
    expect(get).toHaveBeenCalledWith('/v1/version/');
    expect(screen.getByRole('link', { name: 'PRomop home' })).toContainElement(marker);
  });

  it('fetches once for every logo on the page', async () => {
    get.mockResolvedValue({ data: { commit, short: commit.slice(0, 8) } });
    render(<MemoryRouter><BrandLogo /><BrandLogo size="toolbar" /></MemoryRouter>);
    expect(await screen.findAllByTestId('deployed-commit')).toHaveLength(2);
    expect(get).toHaveBeenCalledTimes(1);
  });

  it('shows nothing when the build is unknown or the endpoint is unreachable', async () => {
    get.mockRejectedValue(new Error('offline'));
    render(<MemoryRouter><BrandLogo /></MemoryRouter>);
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(screen.queryByTestId('deployed-commit')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'PRomop home' })).toBeInTheDocument();
  });
});
