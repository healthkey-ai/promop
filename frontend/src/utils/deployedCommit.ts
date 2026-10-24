/**
 * The commit this build runs, from GET /v1/version/ (#2016). Fetched once per
 * page load and shared by every logo on the page; an unreachable endpoint
 * simply shows no commit.
 *
 * It is a bare `fetch`, not one of the axios clients. The logo is on the
 * sign-in, invite and password pages, before anyone is authenticated, so it
 * has no business pulling in the authenticated client and its import-time
 * interceptors; and sharing the public client made every page that mocks it
 * answer the probe as well as its own requests (#2018). The endpoint is
 * public, so no credentials are sent.
 */
import { useEffect, useState } from "react";

export type DeployedCommit = { commit: string; short: string };

const NONE: DeployedCommit = { commit: "", short: "" };
const VERSION_URL = `${import.meta.env.VITE_API_URL || "/api"}/v1/version/`;

let cached: DeployedCommit | null = null;
let pending: Promise<DeployedCommit> | null = null;

export function fetchDeployedCommit(): Promise<DeployedCommit> {
  if (cached) return Promise.resolve(cached);
  if (!pending) {
    // A probe for a version label must never take a page down with it: a
    // failure of any kind reads as "unknown build", and nothing is cached so
    // a later mount may try again.
    try {
      pending = fetch(VERSION_URL, { credentials: "omit", headers: { Accept: "application/json" } })
        .then(response => (response.ok ? response.json() : Promise.reject(new Error(String(response.status)))))
        .then((data: Partial<DeployedCommit> | null) => {
          cached = { commit: data?.commit ?? "", short: data?.short ?? "" };
          return cached;
        })
        .catch(() => { pending = null; return NONE; });
    } catch {
      return Promise.resolve(NONE);
    }
  }
  return pending;
}

/** For tests: forget what was fetched. */
export function resetDeployedCommit() { cached = null; pending = null; }

export function useDeployedCommit(): DeployedCommit {
  const [value, setValue] = useState<DeployedCommit>(cached ?? NONE);
  useEffect(() => {
    let active = true;
    void fetchDeployedCommit().then(found => { if (active) setValue(found); });
    return () => { active = false; };
  }, []);
  return value;
}
