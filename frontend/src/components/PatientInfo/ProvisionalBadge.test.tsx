import { fireEvent, render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import ProvisionalBadge, { AcceptedBadge, MarkedControl } from './ProvisionalBadge';
import { ProvisionalContext, type ProvisionalValue } from './ProvisionalContext';

/** The confirm part of the context a professional's page provides (#2870). */
const confirmContext = (over: Partial<NonNullable<ProvisionalValue['confirm']>> = {}) => ({
  confirmable: new Set(['bmi', 'hemoglobin_g_dl']),
  busy: null,
  errors: {},
  onConfirm: vi.fn(),
  ...over,
});

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

describe('AcceptedBadge', () => {
  it('says Accepted, in green, for a field with no provisional mark', () => {
    render(
      <ProvisionalContext.Provider value={{ marks: { bmi: 74 }, patientMode: false }}>
        <AcceptedBadge name="median_daily_steps_30d" />
      </ProvisionalContext.Provider>,
    );
    const badge = screen.getByTestId('accepted-median_daily_steps_30d');
    expect(badge.textContent).toBe('Accepted');
    expect(badge).toHaveClass('bg-green-100', 'text-green-800');
    expect(badge.title).toMatch(/code mapping is approved/);
  });

  it('is silent for a field marked provisional', () => {
    render(
      <ProvisionalContext.Provider value={{ marks: { median_daily_steps_30d: 70, vo2_max_avg_30d: 82 }, patientMode: false }}>
        <AcceptedBadge name="median_daily_steps_30d" />
        <AcceptedBadge name="vo2_max_avg_30d" />
      </ProvisionalContext.Provider>,
    );
    expect(screen.queryByTestId('accepted-median_daily_steps_30d')).not.toBeInTheDocument();
    expect(screen.queryByTestId('accepted-vo2_max_avg_30d')).not.toBeInTheDocument();
  });
});

describe('ProvisionalBadge for a summary of device readings (#2835)', () => {
  it('says approval settles it, and never to edit the field', () => {
    render(
      <ProvisionalContext.Provider value={{ marks: { median_daily_steps_30d: 85 }, patientMode: false }}>
        <ProvisionalBadge name="median_daily_steps_30d" fromDevice />
      </ProvisionalContext.Provider>,
    );
    const badge = screen.getByTestId('provisional-median_daily_steps_30d');
    expect(badge).toHaveTextContent('Provisional 85%');
    expect(badge.title).toMatch(/device readings whose code mapping is not approved yet \(85% confidence\)/);
    expect(badge.title).toMatch(/becomes accepted when that mapping is approved/);
    expect(badge.title).not.toMatch(/Edit/);
  });

  it('tells a patient it is unconfirmed without asking them to enter it', () => {
    render(
      <ProvisionalContext.Provider value={{ marks: { median_daily_steps_30d: 70 }, patientMode: true }}>
        <ProvisionalBadge name="median_daily_steps_30d" fromDevice />
      </ProvisionalContext.Provider>,
    );
    const badge = screen.getByTestId('provisional-median_daily_steps_30d');
    expect(badge.title).toMatch(/Not yet confirmed/);
    expect(badge.title).not.toMatch(/enter it yourself/);
  });
});

describe('AcceptedBadge wording (#2835)', () => {
  it('speaks of the code mapping to a provider and never to a patient', () => {
    const { unmount } = render(
      <ProvisionalContext.Provider value={{ marks: {}, patientMode: false }}>
        <AcceptedBadge name="median_daily_steps_30d" />
      </ProvisionalContext.Provider>,
    );
    expect(screen.getByTestId('accepted-median_daily_steps_30d').title).toMatch(/code mapping is approved/);
    unmount();
    render(
      <ProvisionalContext.Provider value={{ marks: {}, patientMode: true }}>
        <AcceptedBadge name="median_daily_steps_30d" />
      </ProvisionalContext.Provider>,
    );
    const title = screen.getByTestId('accepted-median_daily_steps_30d').title;
    expect(title).toMatch(/your device readings/);
    expect(title).not.toMatch(/mapping/i);
  });
});

describe('the Confirm control (#2870)', () => {
  it('is an explicit button named for the field, and it confirms that field', () => {
    const confirm = confirmContext();
    render(
      <ProvisionalContext.Provider value={{ marks: { hemoglobin_g_dl: 82 }, patientMode: false, confirm }}>
        <ProvisionalBadge name="hemoglobin_g_dl" label="Hemoglobin (g/dL)" />
      </ProvisionalContext.Provider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Confirm Hemoglobin (g/dL)' }));

    expect(confirm.onConfirm).toHaveBeenCalledWith('hemoglobin_g_dl');
  });

  it('is not shown to a patient', () => {
    render(
      <ProvisionalContext.Provider value={{ marks: { bmi: 74 }, patientMode: true, confirm: confirmContext() }}>
        <ProvisionalBadge name="bmi" />
      </ProvisionalContext.Provider>,
    );
    expect(screen.getByTestId('provisional-bmi')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('is not shown for a field the server did not list', () => {
    render(
      <ProvisionalContext.Provider value={{ marks: { weight: 70 }, patientMode: false, confirm: confirmContext() }}>
        <ProvisionalBadge name="weight" />
      </ProvisionalContext.Provider>,
    );
    expect(screen.getByTestId('provisional-weight')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('is not shown where the value is not provisional', () => {
    render(
      <ProvisionalContext.Provider value={{ marks: {}, patientMode: false, confirm: confirmContext() }}>
        <ProvisionalBadge name="bmi" />
      </ProvisionalContext.Provider>,
    );
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('is not shown on a device-reading summary, which its mapping settles', () => {
    render(
      <ProvisionalContext.Provider value={{ marks: { bmi: 74 }, patientMode: false, confirm: confirmContext() }}>
        <ProvisionalBadge name="bmi" fromDevice />
      </ProvisionalContext.Provider>,
    );
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('is disabled while a confirmation runs and shows the last refusal for its field', () => {
    render(
      <ProvisionalContext.Provider
        value={{ marks: { bmi: 74 }, patientMode: false, confirm: confirmContext({ busy: '*', errors: { bmi: 'Changed.' } }) }}
      >
        <ProvisionalBadge name="bmi" label="BMI" />
      </ProvisionalContext.Provider>,
    );
    expect(screen.getByRole('button', { name: 'Confirm BMI' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('Changed.');
  });

  it('is on a control outside Field too (the GELF checklist)', () => {
    render(
      <ProvisionalContext.Provider
        value={{
          marks: { gelf_criteria_options: 80 },
          patientMode: false,
          confirm: confirmContext({ confirmable: new Set(['gelf_criteria_options']) }),
        }}
      >
        <MarkedControl name="gelf_criteria_options" label="GELF criteria"><input aria-label="gelf" /></MarkedControl>
      </ProvisionalContext.Provider>,
    );
    expect(screen.getByRole('button', { name: 'Confirm GELF criteria' })).toBeInTheDocument();
  });
});
