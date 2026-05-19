export interface PortalBranding {
  appName: string;
  brandHsl: string;
  brandHoverHsl: string;
  fontFamily: string;
  logoUrl?: string;
}

// ── Presets ───────────────────────────────────────────────────────────────────

export const defaultBranding: PortalBranding = {
  appName: '',
  brandHsl: '212 87% 33%',
  brandHoverHsl: '212 95% 28%',
  fontFamily: 'Inter, system-ui, sans-serif',
};

export const healthTreeBranding: PortalBranding = {
  appName: 'HealthTree',
  brandHsl: '159 72% 30%',
  brandHoverHsl: '159 72% 25%',
  fontFamily: 'Inter, system-ui, sans-serif',
  // logoUrl: '/logos/healthtree.svg',
};

export const cancerBotBranding: PortalBranding = {
  appName: 'CancerBot',
  brandHsl: '212 87% 33%',
  brandHoverHsl: '212 95% 28%',
  fontFamily: 'Inter, system-ui, sans-serif',
  // logoUrl: '/logos/cancerbot.svg',
};

// ── Active branding (set once at startup, read anywhere) ─────────────────────

let _active: PortalBranding = defaultBranding;

export function getActiveBranding(): PortalBranding {
  return _active;
}

/**
 * Call once in index.tsx before ReactDOM.render.
 * To switch brands, change that one call — nothing else needs touching.
 *
 *   applyBranding(defaultBranding);   // neutral
 *   applyBranding(healthTreeBranding); // HealthTree green
 *   applyBranding(cancerBotBranding);  // CancerBot blue
 */
export function applyBranding(b: PortalBranding): void {
  _active = b;
  const r = document.documentElement;
  r.style.setProperty('--portal-brand',       b.brandHsl);
  r.style.setProperty('--portal-brand-hover', b.brandHoverHsl);
  document.body.style.fontFamily = b.fontFamily;
}
