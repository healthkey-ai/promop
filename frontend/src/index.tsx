import React from 'react';
import ReactDOM from 'react-dom/client';
import './index.css';
import App from './App';
import { applyBranding, defaultBranding, cancerBotBranding, healthTreeBranding } from './config/branding';

// ─── BRANDING ────────────────────────────────────────────────────────────────
// To switch themes, change the argument here. Available presets:
//   defaultBranding    — neutral blue portal
//   healthTreeBranding — HealthTree green
//   cancerBotBranding  — CancerBot blue
// ─────────────────────────────────────────────────────────────────────────────
applyBranding(defaultBranding);

const root = ReactDOM.createRoot(
  document.getElementById('root') as HTMLElement
);

root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
