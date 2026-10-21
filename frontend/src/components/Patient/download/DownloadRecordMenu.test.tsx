import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import DownloadRecordMenu from './DownloadRecordMenu';

const get = vi.fn();
vi.mock('@/api/clinicalTransport', () => ({
  clinicalClient: () => ({ get }),
  clinicalUrl: (path: string) => path,
}));

let saved: { blob: Blob; name: string }[] = [];

beforeEach(() => {
  saved = [];
  get.mockReset();
  let current: Blob | null = null;
  vi.stubGlobal('URL', Object.assign(URL, {
    createObjectURL: vi.fn((blob: Blob) => { current = blob; return 'blob:x'; }),
    revokeObjectURL: vi.fn(),
  }));
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    saved.push({ blob: current!, name: this.download });
  });
});

afterEach(() => vi.restoreAllMocks());

const renderMenu = (patientMode = false) => render(
  <DownloadRecordMenu
    personId={42}
    patientMode={patientMode}
    record={{ disease: 'Multiple Myeloma', weight: 70 }}
    name="Ada Lovelace"
    diseaseType="myeloma"
    diseaseLabel="Multiple Myeloma"
  />,
);

const choose = async (option: RegExp) => {
  fireEvent.click(screen.getByRole('button', { name: /download/i }));
  fireEvent.click(await screen.findByRole('menuitem', { name: option }));
};

describe('DownloadRecordMenu', () => {
  it('offers PDF and FHIR', async () => {
    renderMenu();
    fireEvent.click(screen.getByRole('button', { name: /download/i }));
    expect(await screen.findByRole('menuitem', { name: /PDF report/ })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: /FHIR R4 bundle/ })).toBeInTheDocument();
  });

  it('saves the signed FHIR export byte for byte', async () => {
    const body = new Blob(['{"resourceType":"Bundle"}'], { type: 'application/fhir+json' });
    get.mockResolvedValue({ data: body });
    renderMenu();
    await choose(/FHIR R4 bundle/);
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(get).toHaveBeenCalledWith('/v1/patient-records/42/export-fhir/', { responseType: 'blob' });
    expect(saved[0].name).toBe('patient-42-record.fhir.json');
    expect(await saved[0].blob.text()).toBe('{"resourceType":"Bundle"}');
  });

  it('builds the PDF from the record and the genomics list', async () => {
    get.mockResolvedValue({ data: [{ id: 1, genomic_feature: 'TP53', variant_name: 'R175H' }] });
    renderMenu();
    await choose(/PDF report/);
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(get).toHaveBeenCalledWith('/v1/patient-records/42/genomics/', { params: undefined });
    // Staff do not get the patient-only Allergies tab, so it is not fetched.
    expect(get).not.toHaveBeenCalledWith('/v1/allergies/', expect.anything());
    expect(saved[0].name).toBe('patient-42-record.pdf');
    expect(saved[0].blob.type).toBe('application/pdf');
    const text = await saved[0].blob.text();
    expect(text.startsWith('%PDF-1.4')).toBe(true);
    expect(text).toContain('(TP53) Tj');
    expect(text).toContain('(Multiple Myeloma) Tj');
  });

  it("names the patient's own copy for them and includes their allergies", async () => {
    get.mockImplementation((url: string) => Promise.resolve({
      data: url.includes('allergies') ? [{ allergen_name: 'Penicillin' }] : [],
    }));
    renderMenu(true);
    await choose(/PDF report/);
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(saved[0].name).toBe('my-health-record.pdf');
    expect(await saved[0].blob.text()).toContain('(Penicillin) Tj');
  });

  it('reports a failed download', async () => {
    get.mockRejectedValue(new Error('403'));
    renderMenu();
    await choose(/FHIR R4 bundle/);
    expect(await screen.findByRole('alert')).toHaveTextContent(/could not download/i);
    expect(saved).toHaveLength(0);
  });
});
