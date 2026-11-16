import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import MultiSelectControl from './MultiSelectControl';

const options = [
  { value: 'none', label: 'No planned therapy' },
  { value: 'surgery', label: 'Surgery' },
  { value: 'chemotherapy', label: 'Chemotherapy' },
];

async function choose(selected: string[], choice: string) {
  const onChange = vi.fn();
  const user = userEvent.setup();
  render(<MultiSelectControl options={options} selectedValues={selected}
    display={selected.join(', ') || 'Select…'} onChange={onChange} />);
  await user.click(screen.getByRole('button'));
  await user.click(screen.getByText(choice));
  return onChange;
}

describe('the exclusive none answer', () => {
  it('replaces the other selected therapies', async () => {
    const onChange = await choose(['surgery'], 'No planned therapy');

    expect(onChange).toHaveBeenLastCalledWith(['none']);
  });

  it('is removed when a real therapy is selected', async () => {
    const onChange = await choose(['none'], 'Surgery');

    expect(onChange).toHaveBeenLastCalledWith(['surgery']);
  });

  it('can be deselected to leave an empty answer', async () => {
    const onChange = await choose(['none'], 'No planned therapy');

    expect(onChange).toHaveBeenLastCalledWith([]);
  });
});
