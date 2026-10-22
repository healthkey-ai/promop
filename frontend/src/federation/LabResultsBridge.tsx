/**
 * Framework-agnostic mount for LabResults (see bridgeClient.ts).
 * `./LabResults` is unchanged, so React hosts such as ht-phr are unaffected.
 */
import { useMemo } from "react";
import { createBridgeComponent } from "@module-federation/bridge-react/v19";

import LabResults from "./LabResults";
import type { LabResultsProps } from "./types";
import { buildBridgeClient, type BridgeConnectionProps } from "./bridgeClient";

export interface LabResultsBridgeProps
  extends Omit<LabResultsProps, "apiClient" | "queryClient" | "apiBasePath">,
    BridgeConnectionProps {}

// The default export is a Module Federation provider factory, not a component,
// so this file can never take part in Fast Refresh: the host loads it through
// `loadRemote`, and a refresh boundary here would have nothing to update.
// eslint-disable-next-line react-refresh/only-export-components
function LabResultsBridgeRoot({
  baseUrl,
  apiBasePath = "/api",
  getToken,
  ...rest
}: LabResultsBridgeProps) {
  const apiClient = useMemo(
    () => buildBridgeClient(baseUrl, apiBasePath, getToken),
    [baseUrl, apiBasePath, getToken],
  );

  return <LabResults apiClient={apiClient} apiBasePath="" {...rest} />;
}

export default createBridgeComponent({ rootComponent: LabResultsBridgeRoot });
