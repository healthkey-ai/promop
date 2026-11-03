import { useState, useEffect, useContext } from "react";
import api from "@/api/axios";
import { PatientInfoContext } from "@/federation/PatientInfoContext";

let cache: string[] | null = null;

/**
 * Fetches the managed disease category labels from `/v1/disease-categories/`
 * and returns them as a flat string array suitable for a select dropdown.
 * Results are cached for the lifetime of the page.
 */
export const useDiseaseCategories = (): { options: string[]; loading: boolean } => {
  const [options, setOptions] = useState<string[]>(cache ?? []);
  const [loading, setLoading] = useState(cache === null);
  const ctx = useContext(PatientInfoContext);
  const client = ctx?.apiClient ?? api;

  useEffect(() => {
    if (cache !== null) return;

    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount
    setLoading(true);

    client
      .get("/v1/disease-categories/")
      .then((res) => {
        if (cancelled) return;
        const labels = (res.data as { value: string; label: string }[]).map(
          (item) => item.label,
        );
        cache = labels;
        setOptions(labels);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [client]);

  return { options, loading };
};
