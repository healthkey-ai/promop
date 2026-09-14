import { Link } from 'react-router-dom';
import { getActiveBranding } from '@/config/branding';

export default function BrandLogo({ size = 'page' }: { size?: 'page' | 'toolbar' }) {
  const branding = getActiveBranding();
  return (
    <Link to="/" aria-label={`${branding.appName} home`}
      className="inline-flex shrink-0 items-center rounded-sm focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-primary">
      {branding.logoUrl ? (
        <img src={branding.logoUrl} alt={branding.appName}
          className={size === 'toolbar' ? 'h-8 w-auto object-contain' : 'h-14 w-auto object-contain sm:h-16'} />
      ) : (
        <span className="text-2xl font-bold text-portal-brand">{branding.appName}</span>
      )}
    </Link>
  );
}
