import { useContext } from 'react';
import { Globe } from 'lucide-react';
import { Link } from 'react-router-dom';
import { FieldMappingLinksContext, fieldMappingHref } from './fieldMappingLinks';

/** A field's globe: opens its Field Concept Mapping, for admins only. */
export default function FieldMappingLink({ name, label }: { name: string; label: string }) {
  if (!useContext(FieldMappingLinksContext)) return null;
  return (
    <Link
      to={fieldMappingHref(name)}
      aria-label={`Field concept mapping for ${label}`}
      title={`Field concept mapping for ${name}`}
      className="inline-flex text-portal-text-secondary hover:text-portal-text-primary"
    >
      <Globe size={12} aria-hidden="true" />
    </Link>
  );
}
