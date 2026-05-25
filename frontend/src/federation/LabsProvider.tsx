import { useMemo, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { AxiosInstance } from "axios";
import { LabsContext } from "./LabsContext";
import type { LabsThemeTokens } from "./types";
import { injectStyles } from "./injectStyles";
import { assertLabsTokens } from "./assertLabsTokens";

injectStyles();
assertLabsTokens();

interface LabsProviderProps {
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
    "--hk-labs-brand-700": merged.colorPrimary,
    "--hk-labs-text-brand": merged.colorPrimary,
    "--hk-labs-success-700": merged.colorSuccess,
    "--hk-labs-warning-700": merged.colorWarning,
    "--hk-labs-error-700": merged.colorDanger,
    "--hk-labs-text-tertiary": merged.colorMuted,
    "--hk-labs-radius": merged.borderRadius,
  };
}

export function LabsProvider({
  apiClient,
  apiBasePath = "",
  queryClient: externalQC,
  theme,
  className,
  children,
}: LabsProviderProps) {
  const internalQC = useMemo(
    () => new QueryClient({
      defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
    }),
    [],
  );

  const qc = externalQC ?? internalQC;
  const cssVars = themeToVars(theme ?? {});

  const content = (
    <LabsContext.Provider value={{ apiClient, apiBasePath }}>
      {children}
    </LabsContext.Provider>
  );

  return (
    <div
      className={`hk-labs-root ${className ?? ""}`}
      style={cssVars as React.CSSProperties}
    >
      <QueryClientProvider client={qc}>{content}</QueryClientProvider>
    </div>
  );
}
