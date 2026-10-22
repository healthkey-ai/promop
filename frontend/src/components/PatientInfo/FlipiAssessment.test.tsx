import { useState } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import FlipiAssessment from './FlipiAssessment';
import { selectedFlipiFactors } from './flipiFactors';
import type { FieldDescriptor } from '@/hooks/useWritableFields';
const writable = { writable: true } as FieldDescriptor;
function Harness() {
  const [value, setValue] = useState<unknown>(null);
  return <FlipiAssessment value={value} descriptor={writable} onChange={(_field, next) => setValue(next)} />;
}
describe('FLIPI transparent calculation', () => {
  it('shows the factors, arithmetic, score, and risk live', () => {
    render(<Harness />);
    expect(screen.getByText('FLIPI Score: Not assessed')).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/Age > 60 years/));
    fireEvent.click(screen.getByLabelText(/Ann Arbor stage III or IV/));
    expect(screen.getByText('FLIPI Score: 2 / 5 — Intermediate risk')).toBeInTheDocument();
    expect(screen.getByText('1 + 1 + 0 + 0 + 0 = 2')).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/LDH above the laboratory upper limit of normal/));
    expect(screen.getByText('FLIPI Score: 3 / 5 — High risk')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Record no risk factors'));
    expect(screen.getByText('FLIPI Score: 0 / 5 — Low risk')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Mark not assessed'));
    expect(screen.getByText('FLIPI Score: Not assessed')).toBeInTheDocument();
  });
  it('normalizes legacy selections without double counting or a false zero', () => {
    expect(selectedFlipiFactors('age,age,Elevated LDH')).toEqual(['age', 'ldh']);
    expect(selectedFlipiFactors('invalid')).toBeNull();
  });
  it('respects permissions for all factors', () => {
    render(<FlipiAssessment value="age" descriptor={{ writable: false } as FieldDescriptor} onChange={() => {}} />);
    for (const checkbox of screen.getAllByRole('checkbox')) expect(checkbox).toBeDisabled();
  });
  it.each([0, 3])('displays a historical score of %s without inventing its factors', recordedScore => {
    render(<FlipiAssessment value={null} recordedScore={recordedScore} descriptor={writable} onChange={() => {}} />);
    expect(screen.getByText(`FLIPI Score: ${recordedScore} / 5 — historical`)).toBeInTheDocument();
    expect(screen.getByText(/Factor checklist unavailable/)).toBeInTheDocument();
    expect(screen.queryByText('FLIPI Score: Not assessed')).not.toBeInTheDocument();
    for (const checkbox of screen.getAllByRole('checkbox')) expect(checkbox).not.toBeChecked();
  });
  it('uses an explicit empty assessment instead of a stale historical score', () => {
    render(<FlipiAssessment value="" recordedScore={3} descriptor={writable} onChange={() => {}} />);
    expect(screen.getByText('FLIPI Score: 0 / 5 — Low risk')).toBeInTheDocument();
    expect(screen.queryByText(/historical/)).not.toBeInTheDocument();
  });
});
