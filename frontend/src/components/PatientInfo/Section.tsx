import React from 'react';

interface SectionProps {
  title: string;
  description?: string;
  children: React.ReactNode;
}

export default function Section({ title, description, children }: SectionProps) {
  return (
    <div className="mt-8 pt-8 border-t border-portal-border first:mt-0 first:pt-0 first:border-0">
      <div className="mb-5">
        <h3 className="text-lg font-semibold text-portal-text-primary">{title}</h3>
        {description && (
          <p className="mt-0.5 text-sm text-portal-text-secondary">{description}</p>
        )}
      </div>
      {children}
    </div>
  );
}
