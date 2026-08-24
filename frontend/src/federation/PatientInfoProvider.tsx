import { useMemo, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { AxiosInstance } from "axios";
import { PatientInfoContext } from "./PatientInfoContext";
import { useWritableFields, usePatientDescriptor } from "./patientInfoApi";
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
  const internalQC = useMemo(
    () => new QueryClient({
      defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
    }),
    [],
  );

  const qc = externalQC ?? internalQC;
  const cssVars = themeToVars(theme ?? {});

  return (
    <div
      className={`promop-root ${className ?? ""}`}
      style={cssVars as React.CSSProperties}
    >
      <QueryClientProvider client={qc}>
        {/* Bridge sits INSIDE QueryClientProvider so useWritableFields (a query) has a client. */}
        <ContextBridge apiClient={apiClient} apiBasePath={apiBasePath}>
          {children}
        </ContextBridge>
      </QueryClientProvider>
    </div>
  );
}

function ContextBridge({
  apiClient,
  apiBasePath,
  children,
}: {
  apiClient: AxiosInstance;
  apiBasePath: string;
  children: ReactNode;
}) {
  const writableFields = useWritableFields(apiClient, apiBasePath);
  const descriptor = usePatientDescriptor(apiClient, apiBasePath);
  return (
    <PatientInfoContext.Provider value={{ apiClient, apiBasePath, writableFields, descriptor }}>
      {children}
    </PatientInfoContext.Provider>
  );
}
