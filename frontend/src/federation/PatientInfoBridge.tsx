/**
 * Framework-agnostic mount for PatientInfo (see bridgeClient.ts).
 *
 *     const { default: createProvider } = await loadRemote("labs_results_remote/PatientInfoBridge");
 *     const provider = createProvider();
 *     await provider.render({ moduleName, dom, baseUrl, getToken, ... });
 *     provider.destroy({ moduleName, dom });
 *
 * `./PatientInfo` is unchanged, so React hosts such as ht-phr are unaffected.
 */
import { useMemo } from "react";
import { createBridgeComponent } from "@module-federation/bridge-react/v19";

import PatientInfo from "./PatientInfo";
import type { PatientInfoProps } from "./patientInfoTypes";
import { buildBridgeClient, type BridgeConnectionProps } from "./bridgeClient";

export interface PatientInfoBridgeProps
  extends Omit<PatientInfoProps, "apiClient" | "queryClient" | "apiBasePath">,
    BridgeConnectionProps {}

// The default export is a Module Federation provider factory, not a component,
// so this file can never take part in Fast Refresh: the host loads it through
// `loadRemote`, and a refresh boundary here would have nothing to update.
// eslint-disable-next-line react-refresh/only-export-components
function PatientInfoBridgeRoot({
  baseUrl,
  apiBasePath = "/api",
  getToken,
  ...rest
}: PatientInfoBridgeProps) {
  const apiClient = useMemo(
    () => buildBridgeClient(baseUrl, apiBasePath, getToken),
    [baseUrl, apiBasePath, getToken],
  );

  // apiBasePath is already baked into the client's baseURL, so the component
  // must not prefix request paths with it a second time.
  return <PatientInfo apiClient={apiClient} apiBasePath="" {...rest} />;
}

export default createBridgeComponent({ rootComponent: PatientInfoBridgeRoot });
