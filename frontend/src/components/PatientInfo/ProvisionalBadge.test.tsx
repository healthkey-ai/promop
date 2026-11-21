import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import ProvisionalBadge, { MarkedControl } from './ProvisionalBadge';
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

describe('MarkedControl', () => {
  it('puts the indicator above a control whose field holds a proposal', () => {
    render(
      <ProvisionalContext.Provider value={{ marks: { gelf_criteria_options: 80 }, patientMode: false }}>
        <MarkedControl name="gelf_criteria_options"><input aria-label="control" /></MarkedControl>
      </ProvisionalContext.Provider>,
    );
    const badge = screen.getByTestId('provisional-gelf_criteria_options');
    const control = screen.getByLabelText('control');
    expect(badge).toHaveTextContent('Provisional 80%');
    expect(badge.compareDocumentPosition(control) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('renders the control alone, with no wrapper element, when nothing is proposed', () => {
    const { container } = render(
      <ProvisionalContext.Provider value={{ marks: { bmi: 74 }, patientMode: false }}>
        <MarkedControl name="gelf_criteria_options"><input aria-label="control" /></MarkedControl>
      </ProvisionalContext.Provider>,
    );
    expect(screen.queryByTestId('provisional-gelf_criteria_options')).not.toBeInTheDocument();
    expect(container.children).toHaveLength(1);
    expect(container.firstElementChild).toBe(screen.getByLabelText('control'));
  });

  it('words it for a patient as not yet confirmed', () => {
    render(
      <ProvisionalContext.Provider value={{ marks: { gelf_criteria_options: 80 }, patientMode: true }}>
        <MarkedControl name="gelf_criteria_options"><span>control</span></MarkedControl>
      </ProvisionalContext.Provider>,
    );
    expect(screen.getByTestId('provisional-gelf_criteria_options').title).toMatch(/Not yet confirmed/);
  });
});

describe('ProvisionalBadge via', () => {
  it('names what to edit when the value is calculated from other inputs', () => {
    render(
      <ProvisionalContext.Provider value={{ marks: { flipi_score: 66 }, patientMode: false }}>
        <ProvisionalBadge name="flipi_score" via="the risk factors below" />
      </ProvisionalContext.Provider>,
    );
    expect(screen.getByTestId('provisional-flipi_score').title).toMatch(/Edit the risk factors below to confirm/);
  });
});
