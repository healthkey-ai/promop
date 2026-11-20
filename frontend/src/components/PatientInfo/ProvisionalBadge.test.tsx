import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import ProvisionalBadge from './ProvisionalBadge';
import { ProvisionalContext } from './ProvisionalContext';

describe('ProvisionalBadge', () => {
  it('says provisional with the confidence, and a clinician is told how to confirm', () => {
    render(
      <ProvisionalContext.Provider value={{ marks: { bmi: 74 }, patientMode: false }}>
        <ProvisionalBadge name="bmi" />
      </ProvisionalContext.Provider>,
    );
    const badge = screen.getByTestId('provisional-bmi');
    expect(badge).toHaveTextContent('Provisional 74%');
    expect(badge.title).toMatch(/not approved yet/);
  });

  it('words it for a patient as not yet confirmed', () => {
    render(
      <ProvisionalContext.Provider value={{ marks: { bmi: 74 }, patientMode: true }}>
        <ProvisionalBadge name="bmi" />
      </ProvisionalContext.Provider>,
    );
    expect(screen.getByTestId('provisional-bmi').title).toMatch(/Not yet confirmed/);
  });

  it('renders nothing for a field with no mark', () => {
    render(
      <ProvisionalContext.Provider value={{ marks: { bmi: 74 }, patientMode: false }}>
        <ProvisionalBadge name="weight_kg" />
      </ProvisionalContext.Provider>,
    );
    expect(screen.queryByTestId('provisional-weight_kg')).not.toBeInTheDocument();
  });
});
