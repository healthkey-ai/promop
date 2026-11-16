import { useState, useEffect } from "react";
import { clinicalClient, clinicalUrl } from "@/api/clinicalTransport";

export interface VocabOption {
  value: string;
  label: string;
}

export interface VocabSource {
  name: string;
  url: string;
}

interface CacheEntry {
  options: VocabOption[];
  source: VocabSource | null;
}

const cache = new Map<string, CacheEntry>();

export const useVocabulary = (
  modelName: string,
  valueField: "code" | "title" = "title",
): { options: VocabOption[]; loading: boolean; source: VocabSource | null } => {
  const cacheKey = `${modelName}:${valueField}`;
  const cached = cache.get(cacheKey);
  const [options, setOptions] = useState<VocabOption[]>(cached?.options ?? []);
  const [source, setSource] = useState<VocabSource | null>(cached?.source ?? null);
  const [loading, setLoading] = useState(!cache.has(cacheKey));

  useEffect(() => {
    if (cache.has(cacheKey)) return;

    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount
    setLoading(true);

    // Through the clinical transport, not the raw standalone client: a
    // federated host mounts the widget under its own API prefix and serves the
    // rest of its origin as a static SPA, so a root-relative
    // `/vocabularies/…` request reaches the shell instead of the host's
    // read-only proxy and the option list silently comes back empty (#5599).
    clinicalClient()
      .get(clinicalUrl(`/vocabularies/${modelName}/`))
      .then((res) => {
        if (cancelled) return;
        const data: { code: string | number; title: string; source_name?: string; source_url?: string }[] = res.data;
        const opts: VocabOption[] = data.map((item) => ({
          value: valueField === "code" ? String(item.code) : item.title,
          label: item.title,
        }));
        const first = data[0];
        const src: VocabSource | null =
          first?.source_name && first?.source_url
            ? { name: first.source_name, url: first.source_url }
            : null;
        cache.set(cacheKey, { options: opts, source: src });
        setOptions(opts);
        setSource(src);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [cacheKey, modelName, valueField]);

  return { options, loading, source };
};
