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
      className={`promop-root ${className ?? ""}`}
      style={cssVars as React.CSSProperties}
    >
      <QueryClientProvider client={qc}>{content}</QueryClientProvider>
    </div>
  );
}
