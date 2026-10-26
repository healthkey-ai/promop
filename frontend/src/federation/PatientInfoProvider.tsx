import { useEffect, useMemo, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { AxiosInstance } from "axios";
import { PatientInfoContext } from "./PatientInfoContext";
import { pushClinicalTransport, setClinicalTransport } from "@/api/clinicalTransport";
import { __resetWritableFieldsCache } from "@/hooks/useWritableFields";
import type { LabsThemeTokens } from "./types";
import { injectStyles } from "./injectStyles";
import { assertLabsTokens } from "./assertLabsTokens";
import { PatientFormContext } from './PatientFormContext';
import { patientFormForClient } from './patientFormRuntime';

injectStyles();
assertLabsTokens();

interface PatientInfoProviderProps {
  apiClient: AxiosInstance;
  apiBasePath?: string;
  queryClient?: QueryClient;
  theme?: Partial<LabsThemeTokens>;
  className?: string;
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
  const store = useMemo(() => patientFormForClient(apiClient, apiBasePath), [apiClient, apiBasePath]);
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
      {children}
      </PatientFormContext.Provider>
    </PatientInfoContext.Provider>
  );

  return (
    <div
      className={`promop-root ${className ?? ""}`}
      style={cssVars as React.CSSProperties}
    >
      <QueryClientProvider client={qc}>{content}</QueryClientProvider>
    </div>
  );
}
