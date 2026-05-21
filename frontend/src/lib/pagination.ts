import { useCallback, useState } from "react";

export type PageSize = 3 | 5 | 10 | 50;

const VALID_PAGE_SIZES = new Set<number>([3, 5, 10, 50]);

export function isValidPageSize(n: number): n is PageSize {
  return VALID_PAGE_SIZES.has(n);
}

export function useLocalPagination(defaultPageSize: PageSize = 10) {
  const [page, setPageRaw] = useState(1);
  const [pageSize, setPageSizeRaw] = useState<PageSize>(defaultPageSize);

  const setPage = useCallback((p: number) => setPageRaw(p), []);
  const setPageSize = useCallback((size: PageSize) => {
    setPageSizeRaw(size);
    setPageRaw(1);
  }, []);

  return { page, pageSize, setPage, setPageSize } as const;
}
