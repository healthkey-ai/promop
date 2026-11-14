import { useCallback, useContext, useMemo } from 'react';
import type { AxiosInstance } from 'axios';

import GenomicsFindings, { GenomicsDialogShell } from '@/components/PatientInfo/GenomicsFindings';
import { PatientFormContext, PatientFormRenewContext, usePatientFormSnapshot } from './PatientFormContext';
import type { PatientFormStore } from './patientFormStore';

/**
 * Resolve the patient and the disease context the genomics catalog is keyed on,
 * then let the shared editor draw the dialog.
 *
 * A host that only has a row (CB's trial details) does not know the patient id;
 * the patient-form store does, because it is the linked record the whole
 * federated surface reads. Nothing clinical is passed in by the host.
 *
 * The widget's own client and base path are handed down explicitly: the findings
 * endpoints must not reach for whichever widget registered a global transport
 * last. Providing the store as the form context also makes the writability
 * descriptor come from the record this widget loaded, not from a shared cache.
 */
export default function GenomicsFindingsModal({ store, client, apiBase, readOnly, onClose, onChanged }: {
  store: PatientFormStore;
  client: AxiosInstance;
  apiBase: string;
  readOnly: boolean;
  onClose?: () => void;
  onChanged: () => void;
}) {
  const state = usePatientFormSnapshot(store);
  const renew = useContext(PatientFormRenewContext);
  const base = apiBase.replace(/\/+$/, '');
  // A fresh arrow every render would re-run the findings load on every state
  // change; the editor keys its effect on the resolved URL string instead.
  const url = useMemo(() => (path: string) => `${base}${path}`, [base]);
  // The findings endpoints bypass the store's save queue, so the store still
  // holds the pre-write record. Re-read it before telling the host, as the
  // therapy panel and the Genomics tab do: other surfaces on this connection
  // render from the store. The promise goes back to GenomicsFindings, which
  // awaits it inside its write: the store's listeners stay held (its bypass
  // spans the await) and a host callback that throws is shown as the write's
  // error instead of escaping as an unhandled rejection.
  const changed = useCallback(
    // A read already in flight started before this write; start a new one.
    () => store.load(true).then(() => onChanged()),
    [store, onChanged],
  );

  if (state.loading) {
    return <GenomicsDialogShell title="Genomic findings" onClose={onClose}>
      <p role="status">Loading genomic findings…</p>
    </GenomicsDialogShell>;
  }
  if (state.error || !state.personId) {
    return <GenomicsDialogShell title="Genomic findings" onClose={onClose}>
      <div role="alert">
        {state.identityChanged && renew
          ? 'The linked patient changed. Retry to load the current patient.'
          : 'Could not load the patient record, so genomic findings cannot be edited.'}
        {' '}<button type="button" className="underline" onClick={() => {
          // Another patient answered: a fresh store, not a reload of this one.
          if (state.identityChanged && renew) renew(); else void store.load();
        }}>Retry</button>
      </div>
    </GenomicsDialogShell>;
  }
  return <PatientFormContext.Provider value={store}>
    <GenomicsFindings
      personId={state.personId}
      disease={String(state.values.disease ?? '')}
      readOnly={readOnly || !state.writable}
      variant="modal"
      client={client}
      url={url}
      onClose={onClose}
      onChanged={changed}
    />
  </PatientFormContext.Provider>;
}
