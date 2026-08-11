/**
 * Tests for SelectControl — a stored value must never be hidden (#434)
 *
 * PatientRecord holds what OMOP derived ("T1", "POSITIVE", "AC-T") while the
 * dropdown vocabularies list display titles ("T1: Invasive Tumor <= 2 cm",
 * "ER+", "Trastuzumab (Herceptin)"). A Radix Select shows its placeholder when
 * `value` matches no item, so those fields rendered blank across the whole UI
 * even though every record held the data.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import SelectControl from './SelectControl';

const TNM_OPTIONS = [
  { value: 'T1: Invasive Tumor <= 2 cm', label: 'T1: Invasive Tumor <= 2 cm' },
  { value: 'T2: Invasive Tumor > 2 - 5 cm', label: 'T2: Invasive Tumor > 2 - 5 cm' },
];

describe('SelectControl - values outside the vocabulary', () => {
  it('displays a stored value that is not among the options', () => {
    render(
      <SelectControl value="T1" options={TNM_OPTIONS} onChange={vi.fn()} />,
    );

    expect(screen.getByRole('combobox')).toHaveTextContent('T1');
  });

  it('still shows the placeholder when there is genuinely no value', () => {
    render(
      <SelectControl
        value=""
        options={TNM_OPTIONS}
        placeholder="Select…"
        treatEmptyOptionAsUnknown={false}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('combobox')).toHaveTextContent('Select…');
  });

  it('displays a value that IS among the options using its label', () => {
    render(
      <SelectControl
        value="T2: Invasive Tumor > 2 - 5 cm"
        options={TNM_OPTIONS}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('combobox')).toHaveTextContent(
      'T2: Invasive Tumor > 2 - 5 cm',
    );
  });

  it('lets a real option replace the surfaced value', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <SelectControl value="T1" options={TNM_OPTIONS} onChange={onChange} />,
    );

    await user.click(screen.getByRole('combobox'));
    await user.click(
      screen.getByRole('option', { name: 'T2: Invasive Tumor > 2 - 5 cm' }),
    );

    expect(onChange).toHaveBeenCalledWith('T2: Invasive Tumor > 2 - 5 cm');
  });

  it('re-selecting the surfaced value never clears it', async () => {
    // The surfaced value is absent from the option->value map, so a lookup that
    // falls through to null would wipe the very value this control exists to
    // show. Radix suppresses onValueChange when the choice equals the current
    // value, so the assertion is that nothing was cleared rather than that a
    // particular call was made.
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <SelectControl value="T1" options={TNM_OPTIONS} onChange={onChange} />,
    );

    await user.click(screen.getByRole('combobox'));
    await user.click(screen.getByRole('option', { name: 'T1' }));

    expect(onChange).not.toHaveBeenCalledWith(null);
    expect(screen.getByRole('combobox')).toHaveTextContent('T1');
  });

  it('clearing still works when a surfaced value is present', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <SelectControl
        value="T1"
        options={TNM_OPTIONS}
        allowClear
        clearLabel="— None —"
        onChange={onChange}
      />,
    );

    await user.click(screen.getByRole('combobox'));
    await user.click(screen.getByRole('option', { name: '— None —' }));

    expect(onChange).toHaveBeenCalledWith(null);
  });

  it('does not duplicate an option when the value matches one', async () => {
    const user = userEvent.setup();
    render(
      <SelectControl
        value="T1: Invasive Tumor <= 2 cm"
        options={TNM_OPTIONS}
        onChange={vi.fn()}
      />,
    );

    await user.click(screen.getByRole('combobox'));

    expect(
      screen.getAllByRole('option', { name: 'T1: Invasive Tumor <= 2 cm' }),
    ).toHaveLength(1);
  });
});
