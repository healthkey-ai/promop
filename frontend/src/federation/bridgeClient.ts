/**
 * Shared plumbing for the framework-agnostic bridge entries.
 *
 * The bridge exists for hosts that are not React — HealthTree ONE is SvelteKit.
 * Such a host cannot sensibly hand over a live AxiosInstance, so the bridge
 * props are data-only (`baseUrl` + `getToken`) and the remote builds its own
 * client here. See ht-phr/docs/mf-bridge-proposal.md §6.2.
 */
import axios, { type AxiosInstance } from "axios";

export interface BridgeConnectionProps {
  /** Service origin, e.g. "https://ctomop-staging-….run.app". */
  baseUrl: string;
  /** Appended to baseUrl to form the axios baseURL. promop serves under /api. */
  apiBasePath?: string;
  /** Resolves the caller's bearer token; the host owns authentication. */
  getToken?: () => Promise<string | null | undefined> | string | null | undefined;
}

export function buildBridgeClient(
  baseUrl: string,
  apiBasePath: string,
  getToken?: BridgeConnectionProps["getToken"],
): AxiosInstance {
  const client = axios.create({
    baseURL: `${baseUrl.replace(/\/$/, "")}${apiBasePath}`,
    headers: { "Content-Type": "application/json" },
  });

  client.interceptors.request.use(async (config) => {
    if (!getToken) return config;
    const token = await getToken();
    if (token) config.headers.Authorization = `Bearer ${token}`;
    return config;
  });

  return client;
}
