import { Link } from 'react-router-dom';
import { getActiveBranding } from '@/config/branding';

export default function BrandLogo({ compact = false }: { compact?: boolean }) {
  const branding = getActiveBranding();
  return (
    <Link to="/" aria-label={`${branding.appName} home`}
      className="inline-flex shrink-0 items-center rounded-sm focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-primary">
      {branding.logoUrl ? (
        <img src={branding.logoUrl} alt={branding.appName}
          className={compact ? 'h-14 w-auto object-contain sm:h-16' : 'h-18 w-auto max-w-60 object-contain object-left'} />
      ) : (
        <span className="text-2xl font-bold text-portal-brand">{branding.appName}</span>
      )}
    </Link>
  );
}
