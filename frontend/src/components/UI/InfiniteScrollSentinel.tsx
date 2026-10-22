import { useEffect, useRef } from "react";

interface Props {
  onIntersect: () => void;
  loading?: boolean;
}

/**
 * Invisible sentinel div that fires `onIntersect` when it scrolls into view.
 * Shows a small spinner while `loading` is true.
 */
export function InfiniteScrollSentinel({ onIntersect, loading }: Props) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) onIntersect();
      },
      { rootMargin: "200px" },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [onIntersect]);

  return (
    <div ref={ref} className="flex items-center justify-center py-4">
      {loading && (
        <svg
          className="h-5 w-5 animate-spin text-muted-foreground"
          viewBox="0 0 24 24"
          fill="none"
        >
          <circle
            className="opacity-25"
            cx="12"
            cy="12"
            r="10"
            stroke="currentColor"
            strokeWidth="4"
          />
          <path
            className="opacity-75"
            fill="currentColor"
            d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
          />
        </svg>
      )}
    </div>
  );
}
