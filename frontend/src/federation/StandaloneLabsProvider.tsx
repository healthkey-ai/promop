import type { ReactNode } from 'react';
import { PatientFormContext, PatientFormReadOnlyContext } from './PatientFormContext';
import type { PatientFormStore } from './patientFormStore';

/**
 * The standalone provider page's Labs tab uses the same source-pair editor as
 * the federated surfaces (#2577). The page owns the store so a queued lab save
 * survives switching tabs; this wrapper only publishes it to the controls.
 *
 * `writeEnabled` is the record read's gate, not a second permission model: the
 * shared-form descriptor still marks each field read-only for a caller without
 * write access, and the server refuses the PATCH. The context keeps controls
 * that do not read the descriptor (attestations, pickers) consistent with it.
 */
export default function StandaloneLabsProvider({
  store,
  writeEnabled,
  children,
}: {
  store: PatientFormStore;
  writeEnabled: boolean;
  children: ReactNode;
}) {
  return (
    <PatientFormContext.Provider value={store}>
      <PatientFormReadOnlyContext.Provider value={!writeEnabled}>
        {children}
      </PatientFormReadOnlyContext.Provider>
    </PatientFormContext.Provider>
  );
}
