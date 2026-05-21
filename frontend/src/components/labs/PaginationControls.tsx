import { ChevronLeft, ChevronRight } from "lucide-react";
import { useMemo } from "react";

import { Button } from "@/components/ui-labs/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui-labs/select";
import { isValidPageSize, type PageSize } from "@/lib/pagination";

const DEFAULT_PAGE_SIZES: PageSize[] = [10, 50];

interface PaginationControlsProps {
  page: number;
  pageSize: PageSize;
  totalCount: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: PageSize) => void;
  pageSizes?: number[];
}

function getPageNumbers(current: number, total: number): (number | "...")[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);

  const pages: (number | "...")[] = [1];

  if (current <= 3) {
    pages.push(2, 3, 4, "...", total);
  } else if (current >= total - 2) {
    pages.push("...", total - 3, total - 2, total - 1, total);
  } else {
    pages.push("...", current - 1, current, current + 1, "...", total);
  }

  return pages;
}

export function PaginationControls({
  page,
  pageSize,
  totalCount,
  onPageChange,
  onPageSizeChange,
  pageSizes,
}: PaginationControlsProps) {
  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));
  const start = (page - 1) * pageSize + 1;
  const end = Math.min(page * pageSize, totalCount);
  const pageNumbers = useMemo(() => getPageNumbers(page, totalPages), [page, totalPages]);
  const availableSizes = (pageSizes ?? DEFAULT_PAGE_SIZES).filter(isValidPageSize);

  return (
    <div className="flex flex-col items-center gap-3 text-sm text-muted-foreground sm:flex-row sm:justify-between">
      <span>
        {totalCount === 0
          ? "No results"
          : `Showing ${start}–${end} of ${totalCount}`}
      </span>

      <div className="flex items-center gap-1">
        <Button
          variant="ghost"
          size="sm"
          className="h-8 w-8 p-0"
          disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}
          aria-label="Previous page"
        >
          <ChevronLeft className="h-4 w-4" />
        </Button>
        {pageNumbers.map((p, i) =>
          p === "..." ? (
            <span key={`ellipsis-${i}`} className="px-1">
              ...
            </span>
          ) : (
            <Button
              key={p}
              variant={p === page ? "primary" : "ghost"}
              size="sm"
              className="h-8 w-8 p-0"
              onClick={() => onPageChange(p)}
              aria-label={`Page ${p}`}
              aria-current={p === page ? "page" : undefined}
            >
              {p}
            </Button>
          ),
        )}
        <Button
          variant="ghost"
          size="sm"
          className="h-8 w-8 p-0"
          disabled={page >= totalPages}
          onClick={() => onPageChange(page + 1)}
          aria-label="Next page"
        >
          <ChevronRight className="h-4 w-4" />
        </Button>
      </div>

      <div className="flex items-center gap-2">
        <span className="hidden sm:inline">Rows per page</span>
        <Select
          value={String(pageSize)}
          onValueChange={(v) => onPageSizeChange(Number(v) as PageSize)}
        >
          <SelectTrigger className="h-8 w-[70px] text-sm">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {availableSizes.map((s) => (
              <SelectItem key={s} value={String(s)}>{s}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </div>
  );
}
