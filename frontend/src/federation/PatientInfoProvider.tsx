import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { AxiosInstance } from "axios";
import { PatientInfoContext } from "./PatientInfoContext";
import { pushClinicalTransport, setClinicalTransport } from "@/api/clinicalTransport";
import { __resetWritableFieldsCache } from "@/hooks/useWritableFields";
import type { LabsThemeTokens } from "./types";
import { injectStyles } from "./injectStyles";
import { assertLabsTokens } from "./assertLabsTokens";
import { PatientFormContext, PatientFormRenewContext } from './PatientFormContext';
import {
  noteRetiredDrafts, patientFormForClient, patientFormStoreId,
  renewPatientFormForClient, takeRetiredDraftCount,
} from './patientFormRuntime';
import type { PatientFormStore } from './patientFormStore';

injectStyles();
assertLabsTokens();

interface PatientInfoProviderProps {
  apiClient: AxiosInstance;
  apiBasePath?: string;
  queryClient?: QueryClient;
  theme?: Partial<LabsThemeTokens>;
  className?: string;
  /** Patient/account context inside the client; see PatientInfoProps. */
  connectionKey?: string;
  children: ReactNode;
}

/*
 * Inline vars are written ONLY for tokens the consumer explicitly passes.
 * Defaults must stay in labs.css's @layer :root fallbacks: an inline style
 * outranks every host stylesheet, so merging defaults here would paint the
 * remote HealthKey blue even in hosts that re-skin the --promop-* contract
 * via their unlayered :root (ht-phr, phr).
 */
function themeToVars(theme: Partial<LabsThemeTokens>): Record<string, string> {
  const vars: Record<string, string> = {};
  if (theme.colorPrimary) {
    vars["--promop-brand-700"] = theme.colorPrimary;
    vars["--promop-text-brand"] = theme.colorPrimary;
  }
  if (theme.colorSuccess) vars["--promop-success-700"] = theme.colorSuccess;
  if (theme.colorWarning) vars["--promop-warning-700"] = theme.colorWarning;
  if (theme.colorDanger) vars["--promop-error-700"] = theme.colorDanger;
  if (theme.colorMuted) vars["--promop-text-tertiary"] = theme.colorMuted;
  if (theme.borderRadius) vars["--promop-radius"] = theme.borderRadius;
  return vars;
}

export function PatientInfoProvider({
  apiClient,
  apiBasePath = "",
  queryClient: externalQC,
  theme,
  className,
  connectionKey = "",
  children,
}: PatientInfoProviderProps) {
  // Point the OMOP write helpers at the host's client, and do it during render
  // rather than in an effect: the tabs below fetch the writable-field descriptor
  // as they mount, which happens before a parent effect runs. A descriptor
  // fetched through the wrong client fails, and a failed descriptor renders every
  // clinical field read-only — the tab would come up uneditable.
  //
  // The descriptor is deployment metadata, so a cache filled under one transport
  // does not describe the next one.
  useMemo(() => {
    if (setClinicalTransport(apiClient, apiBasePath)) __resetWritableFieldsCache();
  }, [apiClient, apiBasePath]);

  // Own the transport for this provider's lifetime instead of resetting it on
  // unmount: a page can also mount a therapy-only widget (trial details), and an
  // unconditional reset here — or a later widget's reset — used to strip the host
  // client from whoever was still open.
  useEffect(() => {
    const release = pushClinicalTransport(apiClient, apiBasePath);
    __resetWritableFieldsCache();
    return () => {
      release();
      __resetWritableFieldsCache();
    };
  }, [apiClient, apiBasePath]);

  const internalQC = useMemo(
    () => new QueryClient({
      defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
    }),
    [],
  );

  const qc = externalQC ?? internalQC;
  // Keyed by client, base and the host's connection key. A renewal after the
  // linked patient changed bumps `renewals`, which re-reads the cache entry
  // that renewPatientFormForClient replaced.
  const [renewals, setRenewals] = useState(0);
  // Unsent edits live on the *old* store after a renewal. It is unmounted with
  // the patient it belongs to, so without this the reader never learns the
  // draft was dropped. The count travels through the runtime, because the
  // provider itself may remount between the switch and the notice.
  const [retiredNotice, setRetiredNotice] = useState<string | null>(null);
  const store = useMemo(
    () => {
      void renewals; // a renewal replaced the cache entry; read it again
      return patientFormForClient(apiClient, apiBasePath, connectionKey);
    },
    [apiClient, apiBasePath, connectionKey, renewals],
  );
  const renew = useCallback(() => {
    renewPatientFormForClient(apiClient, apiBasePath, connectionKey, store);
    setRenewals((count) => count + 1);
  }, [apiClient, apiBasePath, connectionKey, store]);
  // A different store on the same client and base means the identity inside
  // that client changed (a new connectionKey, or a renewal). The previous
  // store must not drain its queue through the new credentials. A plain
  // unmount does not get here, so navigation still lets a queue drain.
  const previous = useRef<{ client: AxiosInstance; base: string; store: PatientFormStore } | null>(null);
  useEffect(() => {
    const prior = previous.current;
    if (prior && prior.store !== store && prior.client === apiClient && prior.base === apiBasePath) {
      noteRetiredDrafts(apiClient, prior.store);
    }
    previous.current = { client: apiClient, base: apiBasePath, store };
    // Consume on mount too: a renewal or key switch can retire the store
    // before this provider instance exists.
    const lost = takeRetiredDraftCount(apiClient);
    if (lost) {
      setRetiredNotice(lost === 1
        ? '1 unsent edit for the previous patient was not saved.'
        : `${lost} unsent edits for the previous patient were not saved.`);
    }
  }, [apiClient, apiBasePath, store]);
  useEffect(() => { void store.load(); }, [store]);
  useEffect(() => {
    let previous: unknown;
    const synchronize = () => {
      const state = store.getSnapshot();
      if (state.loading || state.values === previous) return;
      previous = state.values;
      qc.setQueryData(['patient-info', 'me'], (old: Record<string, unknown> | undefined) => ({
        ...old, patient_info: state.values, patient_name: state.values.patient_name,
      }));
    };
    synchronize();
    return store.subscribe(synchronize);
  }, [store, qc]);
  const cssVars = themeToVars(theme ?? {});

  const content = (
    <PatientInfoContext.Provider value={{ apiClient, apiBasePath }}>
      <PatientFormContext.Provider value={store}>
      <PatientFormRenewContext.Provider value={renew}>
      <Fragment key={patientFormStoreId(store)}>{children}</Fragment>
      </PatientFormRenewContext.Provider>
      </PatientFormContext.Provider>
    </PatientInfoContext.Provider>
  );

  return (
    <div
      className={`promop-root ${className ?? ""}`}
      style={cssVars as React.CSSProperties}
    >
      {retiredNotice && (
        <div role="status" className="mb-2 rounded-lg bg-amber-50 px-4 py-2 text-sm text-amber-900">
          {retiredNotice}
          {' '}<button type="button" className="font-semibold underline"
            onClick={() => setRetiredNotice(null)}>Dismiss</button>
        </div>
      )}
      <QueryClientProvider client={qc}>{content}</QueryClientProvider>
    </div>
  );
}
