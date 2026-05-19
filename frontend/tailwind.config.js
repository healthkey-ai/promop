/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./src/**/*.{js,jsx,ts,tsx}",
  ],
  theme: {
    extend: {
      animation: {
        'tab-in': 'tabIn 0.18s ease-out',
        'fade-in': 'fadeIn 0.2s ease-out',
      },
      keyframes: {
        tabIn: {
          from: { opacity: '0', transform: 'translateY(6px)' },
          to:   { opacity: '1', transform: 'translateY(0)' },
        },
        fadeIn: {
          from: { opacity: '0' },
          to:   { opacity: '1' },
        },
      },
      colors: {
        primary: '#2563eb',
        secondary: '#64748b',
        'portal-text-primary':   'hsl(var(--portal-text-primary))',
        'portal-text-secondary': 'hsl(var(--portal-text-secondary))',
        'portal-text-tertiary':  'hsl(var(--portal-text-tertiary))',
        'portal-border':         'hsl(var(--portal-border))',
        'portal-bg':             'hsl(var(--portal-bg))',
        'portal-bg-secondary':   'hsl(var(--portal-bg-secondary))',
        'portal-brand':          'hsl(var(--portal-brand))',
        'portal-brand-hover':    'hsl(var(--portal-brand-hover))',
        // shadcn/ui tokens (required by Radix component classes)
        background:          'hsl(var(--background))',
        foreground:          'hsl(var(--foreground))',
        popover:             { DEFAULT: 'hsl(var(--popover))', foreground: 'hsl(var(--popover-foreground))' },
        muted:               { DEFAULT: 'hsl(var(--muted))',   foreground: 'hsl(var(--muted-foreground))' },
        accent:              { DEFAULT: 'hsl(var(--accent))',  foreground: 'hsl(var(--accent-foreground))' },
        destructive:         { DEFAULT: 'hsl(var(--destructive))', foreground: 'hsl(var(--destructive-foreground))' },
        border:              'hsl(var(--border))',
        input:               'hsl(var(--input))',
        ring:                'hsl(var(--ring))',
      },
    },
  },
  plugins: [],
}