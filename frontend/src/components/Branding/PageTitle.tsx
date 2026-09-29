import type { ReactNode } from 'react';
import BrandLogo from './BrandLogo';

/** The home link and title share one row on application administration pages. */
export default function PageTitle({ children, className = '', meta }: {
  children: ReactNode;
  className?: string;
  meta?: ReactNode;
}) {
  if (!meta) {
    return (
      <div className="flex min-w-0 items-center gap-3 sm:gap-4">
        <BrandLogo />
        <h1 className={`min-w-0 ${className}`}>{children}</h1>
      </div>
    );
  }

  return (
    <div className="flex min-w-0 items-center gap-3 sm:gap-4">
      <BrandLogo />
      <div className="min-w-0">
        <h1 className={className}>{children}</h1>
        {meta && <div className="mt-0.5 leading-none">{meta}</div>}
      </div>
    </div>
  );
}
