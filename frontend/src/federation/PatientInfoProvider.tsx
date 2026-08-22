import { useEffect, useMemo, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { AxiosInstance } from "axios";
import { PatientInfoContext } from "./PatientInfoContext";
import { setClinicalTransport, resetClinicalTransport } from "@/api/clinicalTransport";
import { __resetWritableFieldsCache } from "@/hooks/useWritableFields";
import type { LabsThemeTokens } from "./types";
import { injectStyles } from "./injectStyles";
import { assertLabsTokens } from "./assertLabsTokens";

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

const defaultTheme: LabsThemeTokens = {
  colorPrimary: "212 87% 33%",
  colorSuccess: "152 91% 29%",
  colorWarning: "25 95% 37%",
  colorDanger: "5 79% 40%",
  colorMuted: "218 8% 46%",
  fontFamily: "'Manrope', sans-serif",
  borderRadius: "0.5rem",
};

function themeToVars(theme: Partial<LabsThemeTokens>): Record<string, string> {
  const merged = { ...defaultTheme, ...theme };
  return {
    "--promop-brand-700": merged.colorPrimary,
    "--promop-text-brand": merged.colorPrimary,
    "--promop-success-700": merged.colorSuccess,
    "--promop-warning-700": merged.colorWarning,
    "--promop-error-700": merged.colorDanger,
    "--promop-text-tertiary": merged.colorMuted,
    "--promop-radius": merged.borderRadius,
  };
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

  useEffect(
    () => () => {
      if (resetClinicalTransport()) __resetWritableFieldsCache();
    },
    [],
  );

  const internalQC = useMemo(
    () => new QueryClient({
      defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
    }),
    [],
  );

  const qc = externalQC ?? internalQC;
  const cssVars = themeToVars(theme ?? {});

  const content = (
    <PatientInfoContext.Provider value={{ apiClient, apiBasePath }}>
      {children}
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
