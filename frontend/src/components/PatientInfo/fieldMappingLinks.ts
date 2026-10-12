import { createContext } from 'react';

/**
 * Whether patient-editor fields link to their Field Concept Mapping.
 *
 * Set once by the patient page, for staff and org admins outside patient mode.
 * A context rather than useAuth in each field: useAuth fetches the user per
 * call, and a tab renders dozens of fields. Without a provider it is off, so a
 * field rendered on its own (and its tests) needs no router or user.
 */
export const FieldMappingLinksContext = createContext(false);

export const FieldMappingLinksProvider = FieldMappingLinksContext.Provider;

export const fieldMappingHref = (fieldName: string) =>
  `/field-mappings?field=${encodeURIComponent(fieldName)}`;
