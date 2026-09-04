/**
 * The General tab spans both write targets.
 *
 * Gender, race, ethnicity and the address live on `Person`; the vitals and
 * performance scores are OMOP measurements. Sixteen of its thirty fields are
 * writable and the rest are refused for three different reasons, so this is the
 * tab where "render what the server says" has to mean more than one thing.
 */
import { render, screen, waitFor } from '@testing-library/react';
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
  kind: 'editable', writable: true, target: 'measurement',
  concept_id: 1, code, value_kind: 'number', type_concept_id: 32856,
  source_value: code,
});

const DESCRIPTORS: Record<string, unknown> = {
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
  // Writable only while empty, so not writable as far as an editor is concerned.
  date_of_birth: {
    kind: 'profile', writable: false, fill_if_empty: true, target: 'person',
    reason: 'Set on the Person record, and only while it is empty — this endpoint never overwrites an existing value.',
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

function renderTab(formData: Record<string, unknown> = {}) {
  return render(
    <GeneralTab
      formData={formData}
      onChange={vi.fn()}
      editedName="Alishia Howell"
      onNameChange={vi.fn()}
      onZipcodeChange={vi.fn()}
      diseaseType="myeloma"
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

  it('explains a field that is fillable only while empty', async () => {
    // The endpoint never overwrites a date of birth, so an editable box would
    // lie about the outcome: the save would succeed and change nothing.
    renderTab({ date_of_birth: '1970-01-01' });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByTestId('reason-date_of_birth')).toHaveTextContent(
      /only while it is empty/i,
    );
  });

  it('explains a computed field rather than offering it', async () => {
    renderTab({ bmi: 24.2, height: 170, weight: 70 });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByTestId('reason-bmi')).toHaveTextContent(/computed from/i);
  });

  it('explains an unmapped field rather than offering it', async () => {
    // Twelve of the thirty are unmapped. They were selects and text boxes that
    // returned 405 on every save.
    renderTab({ disease: 'Multiple Myeloma', hiv_status: false });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByTestId('reason-disease')).toBeInTheDocument();
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
                         'Infection Status', 'Physical Measurements']) {
      expect(screen.getByText(title)).toBeInTheDocument();
    }
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
