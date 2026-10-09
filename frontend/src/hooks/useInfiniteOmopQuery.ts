import { useMemo } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { clinicalClient, clinicalUrl } from "@/api/clinicalTransport";
import type { PaginatedResponse } from "@/federation/types";

const DEFAULT_PAGE_SIZE = 100;

/**
 * Reusable hook for paginated fetches against the v1 OMOP clinical endpoints.
 * Returns accumulated results across all fetched pages plus infinite-query controls.
 */
export function useInfiniteOmopQuery<T>(
  endpoint: string,
  personId: number | undefined,
  pageSize = DEFAULT_PAGE_SIZE,
) {
  const query = useInfiniteQuery<PaginatedResponse<T>>({
    queryKey: ["clinical-summary", endpoint, personId, pageSize],
    queryFn: async ({ pageParam }) => {
      // The clinical transport, not `@/api/axios`: that singleton resolves
      // relative URLs against whatever origin serves the bundle, which under a
      // federation host is the HOST. `/v1/measurements/` then returns the
      // host's HTML shell with a 200, `results` is undefined, and the Labs and
      // History tabs crash on the first row that is not there.
      const resp = await clinicalClient().get<PaginatedResponse<T>>(
        clinicalUrl(`/v1/${endpoint}/`),
        { params: { person_id: personId, page: pageParam, page_size: pageSize } },
      );
      return resp.data;
    },
    initialPageParam: 1,
    getNextPageParam: (lastPage, _allPages, lastPageParam) =>
      lastPage.next ? (lastPageParam as number) + 1 : undefined,
    enabled: !!personId,
    staleTime: 30_000,
  });

  // `?? []` per page: a response that is not the paginated shape we asked for
  // otherwise flattens to `[undefined]`, and every consumer reads fields off
  // the rows without checking.
  const allResults = useMemo(
    () => query.data?.pages.flatMap((p) => p.results ?? []) ?? [],
    [query.data?.pages],
  );
  const totalCount = query.data?.pages[0]?.count ?? 0;

  return {
    ...query,
    allResults,
    totalCount,
  };
}
