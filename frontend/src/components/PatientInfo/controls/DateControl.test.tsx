import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import DateControl from './DateControl';

describe('year-only date entry', () => {
  it('allows typing a complete year without submitting partial dates', async () => {
    const onChange = vi.fn();
    render(<DateControl precision="year" value={null} onChange={onChange} />);
    const input = screen.getByRole('spinbutton');
    const user = userEvent.setup();
    await user.type(input, '198');
    expect(input).toHaveValue(198);
    expect(onChange).not.toHaveBeenCalled();
    await user.type(input, '5');
    expect(input).toHaveValue(1985);
    expect(onChange).toHaveBeenCalledExactlyOnceWith('1985-01-01');
  });

  it('restores the saved year after invalid input and follows external values', () => {
    const onChange = vi.fn();
    const { rerender } = render(<DateControl precision="year" value="1985-01-01" onChange={onChange} />);
    const input = screen.getByRole('spinbutton');
    fireEvent.change(input, { target: { value: '9999' } });
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.blur(input);
    expect(input).toHaveValue(1985);
    rerender(<DateControl precision="year" value="2000-01-01" onChange={onChange} />);
    expect(input).toHaveValue(2000);
    fireEvent.change(input, { target: { value: '' } });
    expect(onChange).toHaveBeenCalledExactlyOnceWith(null);
  });
});
