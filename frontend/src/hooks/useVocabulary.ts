import { useState, useEffect } from "react";
import api from "@/api/axios";

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

    api
      .get(`/vocabularies/${modelName}/`)
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
