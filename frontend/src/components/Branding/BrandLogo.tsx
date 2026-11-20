import { Link } from 'react-router-dom';
import { getActiveBranding } from '@/config/branding';
import { useDeployedCommit } from '@/utils/deployedCommit';

export default function BrandLogo({ size = 'page' }: { size?: 'page' | 'toolbar' }) {
  const branding = getActiveBranding();
  // The build under the logo, so a report from staging can name its version (#2016).
  const deployed = useDeployedCommit();
  return (
    <Link to="/" aria-label={`${branding.appName} home`}
      className="inline-flex shrink-0 flex-col items-start rounded-sm focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-primary">
      {branding.logoUrl ? (
        <img src={branding.logoUrl} alt={branding.appName}
          className={size === 'toolbar' ? 'h-8 w-auto object-contain' : 'h-14 w-auto object-contain sm:h-16'} />
      ) : (
        <span className="text-2xl font-bold text-portal-brand">{branding.appName}</span>
      )}
      {deployed.short && (
        <span data-testid="deployed-commit" title={`Deployed commit ${deployed.commit}`}
          className="mt-0.5 font-mono text-[9px] leading-none text-slate-400">{deployed.short}</span>
      )}
    </Link>
  );
}
