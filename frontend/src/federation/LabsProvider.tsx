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
  colorPrimary: "#6366f1",
  colorSuccess: "#22c55e",
  colorWarning: "#f59e0b",
  colorDanger: "#ef4444",
  colorMuted: "#6b7280",
  fontFamily: "'Manrope', sans-serif",
  borderRadius: "0.5rem",
};

function themeToVars(theme: Partial<LabsThemeTokens>): Record<string, string> {
  const merged = { ...defaultTheme, ...theme };
  return {
    "--hk-color-primary": merged.colorPrimary,
    "--hk-color-success": merged.colorSuccess,
    "--hk-color-warning": merged.colorWarning,
    "--hk-color-danger": merged.colorDanger,
    "--hk-color-muted": merged.colorMuted,
    "--hk-font-family": merged.fontFamily,
    "--hk-border-radius": merged.borderRadius,
  };
}

export function LabsProvider({
  apiClient,
  apiBasePath = "/api",
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
