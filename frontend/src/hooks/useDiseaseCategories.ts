import { useState, useEffect, useContext, useMemo, useCallback } from "react";
import api from "@/api/axios";
import { PatientInfoContext } from "@/federation/PatientInfoContext";

interface CategoryObject {
  value: string;
  label: string;
}

let labelCache: string[] | null = null;
let objectCache: CategoryObject[] | null = null;

/**
 * Fetches the managed disease category labels from `/v1/disease-categories/`
 * and returns them as a flat string array suitable for a select dropdown.
 * Results are cached for the lifetime of the page.
 */
export const useDiseaseCategories = (): { options: string[]; loading: boolean } => {
  const [options, setOptions] = useState<string[]>(labelCache ?? []);
  const [loading, setLoading] = useState(labelCache === null);
  const ctx = useContext(PatientInfoContext);
  const client = ctx?.apiClient ?? api;

  useEffect(() => {
    if (labelCache !== null) return;

    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount
    setLoading(true);

    client
      .get("/v1/disease-categories/")
      .then((res) => {
        if (cancelled) return;
        const items = res.data as CategoryObject[];
        const labels = items.map((item) => item.label);
        labelCache = labels;
        objectCache = items;
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

/**
 * Returns disease categories as `{value, label}` objects — for dropdowns that
 * need to send category codes as filter values.
 */
export const useCategoryObjects = (): {
  categories: CategoryObject[];
  loading: boolean;
} => {
  const [categories, setCategories] = useState<CategoryObject[]>(
    objectCache ?? [],
  );
  const [loading, setLoading] = useState(objectCache === null);
  const ctx = useContext(PatientInfoContext);
  const client = ctx?.apiClient ?? api;

  useEffect(() => {
    if (objectCache !== null) return;

    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount
    setLoading(true);

    client
      .get("/v1/disease-categories/")
      .then((res) => {
        if (cancelled) return;
        const items = res.data as CategoryObject[];
        objectCache = items;
        labelCache = items.map((i) => i.label);
        setCategories(items);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [client]);

  return { categories, loading };
};

/**
 * Fetches disease names for a specific category from
 * `/v1/disease-category-mappings/?category=<code>`.
 * Skips the fetch when `category` is null or empty.
 */
export const useDiseasesByCategory = (
  category: string | null,
): { diseases: string[]; loading: boolean } => {
  const [all, setAll] = useState<Record<string, string[]>>({});
  const [loading, setLoading] = useState(false);
  const ctx = useContext(PatientInfoContext);
  const client = ctx?.apiClient ?? api;

  useEffect(() => {
    if (!category) return;
    if (all[category]) return;

    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount
    setLoading(true);

    client
      .get("/v1/disease-category-mappings/", { params: { category } })
      .then((res) => {
        if (cancelled) return;
        const names = (
          res.data as { disease_name: string }[]
        ).map((m) => m.disease_name);
        names.sort();
        setAll((prev) => ({ ...prev, [category]: names }));
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [category, client, all]);

  const diseases = useMemo(
    () => (category ? all[category] ?? [] : []),
    [category, all],
  );

  return { diseases, loading };
};

interface MappingEntry {
  disease_name: string;
  category: string;
}

let allMappingsCache: MappingEntry[] | null = null;

/**
 * Fetches all disease-category mappings and provides a reverse lookup from
 * disease name to category code, plus a grouped map of diseases by category.
 */
export const useAllDiseaseMappings = (): {
  categoryForDisease: (disease: string) => string | null;
  diseasesByCategory: Record<string, string[]>;
  loading: boolean;
} => {
  const [mappings, setMappings] = useState<MappingEntry[]>(
    allMappingsCache ?? [],
  );
  const [loading, setLoading] = useState(allMappingsCache === null);
  const ctx = useContext(PatientInfoContext);
  const client = ctx?.apiClient ?? api;

  useEffect(() => {
    if (allMappingsCache !== null) return;

    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount
    setLoading(true);

    client
      .get("/v1/disease-category-mappings/")
      .then((res) => {
        if (cancelled) return;
        const items = res.data as MappingEntry[];
        allMappingsCache = items;
        setMappings(items);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [client]);

  const reverseLookup = useMemo(() => {
    const map = new Map<string, string>();
    for (const m of mappings) {
      map.set(m.disease_name.toLowerCase(), m.category);
    }
    return map;
  }, [mappings]);

  const categoryForDisease = useCallback(
    (disease: string): string | null =>
      reverseLookup.get(disease.toLowerCase()) ?? null,
    [reverseLookup],
  );

  const diseasesByCategory = useMemo(() => {
    const grouped: Record<string, string[]> = {};
    for (const m of mappings) {
      (grouped[m.category] ??= []).push(m.disease_name);
    }
    for (const v of Object.values(grouped)) {
      v.sort();
    }
    return grouped;
  }, [mappings]);

  return { categoryForDisease, diseasesByCategory, loading };
};

/** Reset all module-level caches — for tests only. */
export function __resetDiseaseCategoryCaches(): void {
  labelCache = null;
  objectCache = null;
  allMappingsCache = null;
}
