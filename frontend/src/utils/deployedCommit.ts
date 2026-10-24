/**
 * The commit this build runs, from GET /v1/version/ (#2016). Fetched once per
 * page load and shared by every logo on the page; an unreachable endpoint
 * simply shows no commit.
 */
import { useEffect, useState } from "react";
import api from "@/api/axios";

export type DeployedCommit = { commit: string; short: string };

let cached: DeployedCommit | null = null;
let pending: Promise<DeployedCommit> | null = null;

export function fetchDeployedCommit(): Promise<DeployedCommit> {
  if (cached) return Promise.resolve(cached);
  pending ??= api.get<DeployedCommit>("/v1/version/")
    .then(({ data }) => { cached = { commit: data?.commit ?? "", short: data?.short ?? "" }; return cached; })
    .catch(() => { pending = null; return { commit: "", short: "" }; });
  return pending;
}

/** For tests: forget what was fetched. */
export function resetDeployedCommit() { cached = null; pending = null; }

export function useDeployedCommit(): DeployedCommit {
  const [value, setValue] = useState<DeployedCommit>(cached ?? { commit: "", short: "" });
  useEffect(() => {
    let active = true;
    void fetchDeployedCommit().then(found => { if (active) setValue(found); });
    return () => { active = false; };
  }, []);
  return value;
}
