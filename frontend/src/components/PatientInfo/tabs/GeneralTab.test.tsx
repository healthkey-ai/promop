/**
 * The General tab spans both write targets.
 *
 * Gender, race, ethnicity and the address live on `Person`; the vitals and
 * performance scores are OMOP measurements. Sixteen of its thirty fields are
 * writable and the rest are refused for three different reasons, so this is the
 * tab where "render what the server says" has to mean more than one thing.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import GeneralTab from './GeneralTab';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';

const mockGet = vi.fn();
vi.mock('@/api/axios', () => ({
  default: { get: (...a: unknown[]) => mockGet(...a), post: vi.fn(), patch: vi.fn() },
}));

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
}));

const measurement = (code: string) => ({
  kind: 'direct', writable: true, target: 'patient_record',
  value_kind: 'number',
  projection: {
    omop_table: 'measurement', concept_id: 1, code,
    type_concept_id: 32856, source_value: code,
  },
});

const DESCRIPTORS: Record<string, unknown> = {
  death_date: { kind: 'direct', writable: true, target: 'patient_record', value_kind: 'date' },
  // Person attributes — no event date, because the record keeps no history of them.
  gender: {
    kind: 'profile', writable: true, target: 'person', payload_field: 'gender',
    value_kind: 'string',
    options: [{ value: 'Female', code: 'F' }, { value: 'Male', code: 'M' },
              { value: 'Unknown', code: 'UNK' }],
  },
  race: {
    kind: 'profile', writable: true, target: 'person', payload_field: 'race',
    value_kind: 'string',
    options: [{ value: 'Asian', code: '2028-9' }, { value: 'White', code: '2106-3' }],
  },
  email: { kind: 'profile', writable: true, target: 'person', payload_field: 'email', value_kind: 'string' },
  city: { kind: 'profile', writable: true, target: 'person', payload_field: 'city', value_kind: 'string' },
  date_of_birth: {
    kind: 'direct', writable: true, target: 'patient_record',
    projection_target: 'person', value_kind: 'date',
  },
  // OMOP measurements — these do carry a date.
  weight: measurement('29463-7'),
  height: measurement('8302-2'),
  systolic_blood_pressure: measurement('8480-6'),
  ecog_performance_status: measurement('89247-1'),
  bmi: {
    kind: 'computed', writable: false, inputs: ['height', 'weight'],
    reason: 'Computed from height, weight.',
  },
  disease: {
    kind: 'unmapped', writable: false, group: 'needs-concept-set',
    reason: 'No concept set assigned yet.',
  },
  hiv_status: {
    kind: 'unmapped', writable: false, group: 'needs-concept-set',
    reason: 'No concept set assigned yet.',
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  mockGet.mockResolvedValue({ data: DESCRIPTORS });
});

function renderTab(
  formData: Record<string, unknown> = {},
  onChange: (field: string, value: unknown) => void = vi.fn(),
) {
  return render(
    <GeneralTab
      formData={formData}
      onChange={onChange}
      editedName="Alishia Howell"
      onNameChange={vi.fn()}
      onZipcodeChange={vi.fn()}
    />,
  );
}

describe('GeneralTab', () => {
  it('fetches the descriptor', async () => {
    renderTab();
    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith(
        '/v1/patient-records/writable-fields/',
        expect.anything(),
      ),
    );
  });

  it('offers a result date for a measurement but not for a Person attribute', async () => {
    // A measurement is an event and needs a date. Gender is not, and offering
    // one would suggest the record keeps a history of it.
    renderTab({ weight: 70, gender: 'Female' });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    const dates = screen.getAllByLabelText('Result date');
    expect(dates.length).toBeGreaterThan(0);
    expect((dates[0] as HTMLInputElement).value).toBe(
      new Date().toISOString().slice(0, 10),
    );
    // One per writable measurement on the tab, and no more.
    const measurements = ['weight', 'height', 'systolic_blood_pressure',
                          'ecog_performance_status'];
    expect(dates).toHaveLength(measurements.length);
  });

  it('renders a writable Person attribute as an editable control', async () => {
    // Gender carries curated options from the descriptor, and GeneralTab passes
    // no local list of its own -- the server's set is the only source. Which set
    // wins when both exist is covered in ClinicalField.test.tsx.
    renderTab({ gender: 'Female' });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByText('Female')).toBeInTheDocument();
    expect(screen.queryByTestId('reason-gender')).not.toBeInTheDocument();
  });

  it('renders date of birth as an editable calendar control', async () => {
    const onChange = vi.fn();
    renderTab({ date_of_birth: '1970-01-01' }, onChange);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    const input = screen.getByText('Date of Birth').parentElement?.parentElement
      ?.querySelector('input') as HTMLInputElement;
    expect(input).toBeEnabled();
    expect(input).toHaveAttribute('type', 'date');
    expect(input).toHaveValue('1970-01-01');
    expect(screen.queryByTestId('reason-date_of_birth')).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: '1971-02-03' } });
    expect(onChange).toHaveBeenCalledWith('date_of_birth', '1971-02-03');
  });

  it('explains a computed field rather than offering it', async () => {
    renderTab({ bmi: 24.2, height: 170, weight: 70 });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByTestId('reason-bmi')).toHaveTextContent(/computed from/i);
  });

  it('keeps disease attributes on the Disease tab rather than duplicating editors', async () => {
    // Twelve of the thirty are unmapped. They were selects and text boxes that
    // returned 405 on every save.
    renderTab({ disease: 'Multiple Myeloma', stage: 'III', histologic_type: 'Plasma cell myeloma', hiv_status: false });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.queryByLabelText('Disease')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Stage')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Histologic Type')).not.toBeInTheDocument();
    expect(screen.getByTestId('reason-hiv_status')).toBeInTheDocument();
  });

  it('leaves the age display alone', async () => {
    // Not a PatientRecord column at all — worked out in the browser from the
    // date of birth.
    renderTab({ date_of_birth: '1970-06-15' });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByText(/calculated from the date of birth/i)).toBeInTheDocument();
  });

  it('keeps the patient name and zip controls, which are not descriptor fields', async () => {
    // patient_name is applied to Person before the serializer sees it, and the
    // zip control auto-fills city and region.
    renderTab({ postal_code: '02114' });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByDisplayValue('Alishia Howell')).toBeInTheDocument();
    expect(screen.getByDisplayValue('02114')).toBeInTheDocument();
  });

  it('fails closed when the descriptor cannot be fetched', async () => {
    // Offering an edit the server will refuse is worse than showing a value that
    // cannot yet change.
    mockGet.mockRejectedValue(new Error('offline'));
    renderTab({ weight: 70, gender: 'Female' });

    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.queryAllByLabelText('Result date')).toHaveLength(0),
    );
  });

  it('keeps every section it had before the conversion', async () => {
    renderTab();
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    for (const title of ['Patient Details', 'Location', 'Race & Ethnicity',
                         'Clinical Summary', 'Medical History',
                         'Infection Status', 'Physical Measurements', 'End of Life']) {
      expect(screen.getByText(title)).toBeInTheDocument();
    }
  });

  it('puts death date at the bottom of the tab', async () => {
    renderTab({ death_date: '2025-02-01' });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    const deathDate = screen.getByText('Death Date').parentElement?.parentElement
      ?.querySelector('input') as HTMLInputElement;
    const physicalMeasurements = screen.getByText('Physical Measurements');
    expect(
      physicalMeasurements.compareDocumentPosition(deathDate)
      & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(deathDate).toHaveAttribute('type', 'date');
  });
});

/**
 * Person fields that no tab showed (plan step 5).
 *
 * Writable on the persons endpoint and invisible, so the write path existed and
 * nothing could reach it. Contact details sit with the other Person attributes;
 * the coordinates sit with the address they are derived from; and the clinician
 * validation flags get their own block, because "has a clinician checked this"
 * is a different question from any of the demographics around it.
 */
describe('GeneralTab — previously unreachable Person fields', () => {
  const profile = (payload_field: string, value_kind = 'string') => ({
    kind: 'profile', writable: true, target: 'person', payload_field, value_kind,
  });

  const PERSON_FIELDS: Record<string, unknown> = {
    phone_number: profile('phone_number'),
    facility_name: profile('facility_name'),
    latitude: profile('latitude', 'number'),
    longitude: profile('longitude', 'number'),
    validated: profile('validated', 'boolean'),
    validated_by: profile('validated_by'),
    validation_date: profile('validation_date', 'date'),
  };

  beforeEach(() => {
    __resetWritableFieldsCache();
    mockGet.mockResolvedValue({ data: PERSON_FIELDS });
  });

  it('renders each of them, and none read-only', async () => {
    renderTab({ phone_number: '617-555-0100', facility_name: 'Dana-Farber' });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    for (const name of Object.keys(PERSON_FIELDS)) {
      expect(screen.queryByTestId(`reason-${name}`)).not.toBeInTheDocument();
    }
    expect(screen.getByDisplayValue('617-555-0100')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Dana-Farber')).toBeInTheDocument();
  });

  it('gives clinician validation its own section', async () => {
    renderTab({ validated_by: 'Dr Chen' });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByText('Clinician Validation')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Dr Chen')).toBeInTheDocument();
  });

  it('offers no result date for a Person attribute', async () => {
    // None of these is an event; a result date would imply a history the record
    // does not keep.
    renderTab({ latitude: 42.36 });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.queryAllByLabelText('Result date')).toHaveLength(0);
  });
});

it('allows correcting the death date', async () => {
  renderTab({ death_date: '2025-02-01' });
  await waitFor(() => expect(screen.getByText('Death Date').parentElement?.parentElement?.querySelector('input')).toBeEnabled());
  expect(screen.getByText('Death Date').parentElement?.parentElement?.querySelector('input')).toHaveValue('2025-02-01');
});
