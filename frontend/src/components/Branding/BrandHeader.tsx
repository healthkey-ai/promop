import { Link } from 'react-router-dom';
import { getActiveBranding } from '@/config/branding';

/** One application masthead, outside page content and child dialogs. */
export default function BrandHeader() {
  const branding = getActiveBranding();
  return (
    <header className="flex h-24 items-center border-b border-border bg-background px-4 sm:px-6">
      <Link to="/" aria-label={`${branding.appName} home`}
        className="inline-flex shrink-0 items-center rounded-sm focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-primary">
        {branding.logoUrl ? (
          <img src={branding.logoUrl} alt={branding.appName}
            className="h-18 w-auto max-w-60 object-contain object-left" />
        ) : (
          <span className="text-2xl font-bold text-portal-brand">{branding.appName}</span>
        )}
      </Link>
    </header>
  );
}
