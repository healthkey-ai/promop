/**
 * BooleanControl — tri-state mode for current-diagnosis answers (#5521).
 *
 * A nullable boolean has three answers: Yes, No and Unknown. The ordinary
 * control only offers Yes/No plus a clear, which maps a deliberate "Unknown"
 * onto the same null as "never answered"; tri-state spells it out.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import BooleanControl from './BooleanControl';

describe('BooleanControl — tri-state', () => {
  it('shows an explicit Unknown option for a null value', () => {
    render(<BooleanControl value={null} triState onChange={vi.fn()} />);

    expect(screen.getByRole('combobox')).toHaveTextContent('Unknown');
  });

  it('still shows Yes and No for stored answers', () => {
    const { rerender } = render(
      <BooleanControl value={true} triState onChange={vi.fn()} />,
    );
    expect(screen.getByRole('combobox')).toHaveTextContent('Yes');

    rerender(<BooleanControl value={false} triState onChange={vi.fn()} />);
    expect(screen.getByRole('combobox')).toHaveTextContent('No');
  });

  it('offers Yes, No and Unknown, and reports a No as false', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<BooleanControl value={null} triState onChange={onChange} />);

    await user.click(screen.getByRole('combobox'));
    expect(screen.getByRole('option', { name: 'Unknown' })).toBeInTheDocument();
    await user.click(screen.getByRole('option', { name: 'No' }));

    expect(onChange).toHaveBeenCalledWith(false);
  });

  it('reports Unknown as null', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<BooleanControl value={true} triState onChange={onChange} />);

    await user.click(screen.getByRole('combobox'));
    await user.click(screen.getByRole('option', { name: 'Unknown' }));

    expect(onChange).toHaveBeenCalledWith(null);
  });

  it('keeps the ordinary two-answer control unchanged', async () => {
    const user = userEvent.setup();
    render(<BooleanControl value={null} onChange={vi.fn()} />);

    await user.click(screen.getByRole('combobox'));
    expect(screen.queryByRole('option', { name: 'Unknown' })).not.toBeInTheDocument();
    expect(screen.getByRole('option', { name: '— None —' })).toBeInTheDocument();
  });
});
