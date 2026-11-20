import { useEffect, useState } from "react";
import api from "@/api/axios";
import type { WearableSourceSystem } from "./codeMappingTypes";

/**
 * The source code systems a wearable file or share can be limited to, as the
 * server lists them in `code-mappings/reference/`. Read once per page load
 * and shared: the Download and Share controls both want it, and a failed read
 * is not remembered, so the next control to mount asks again.
 */
let known: Promise<WearableSourceSystem[]> | null = null;

export function loadWearableSourceSystems(): Promise<WearableSourceSystem[]> {
  if (!known) {
    known = api.get<{ wearable_source_systems?: WearableSourceSystem[] }>("/v1/code-mappings/reference/")
      .then((response) => response?.data?.wearable_source_systems ?? [])
      .catch(() => {
        known = null;
        return [];
      });
  }
  return known;
}

/** `systems` when the page already holds the reference; otherwise asks for it. */
export function useWearableSourceSystems(systems?: WearableSourceSystem[]): WearableSourceSystem[] {
  const [loaded, setLoaded] = useState<WearableSourceSystem[]>([]);
  useEffect(() => {
    if (systems) return undefined;
    let current = true;
    (async () => {
      const result = await loadWearableSourceSystems();
      if (current) setLoaded(result);
    })();
    return () => { current = false; };
  }, [systems]);
  return systems ?? loaded;
}

