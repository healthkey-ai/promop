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

/**
 * Where a patient tab's Add field link opens Field Concept Mapping (#721):
 * create mode, on the tab the link sits on and, from the disease tab, scoped
 * to that disease's category. The URL names a tab and a category and nothing
 * about the patient.
 */
export const addFieldHref = (tab: string, disease?: string) => {
  const params = new URLSearchParams({ new: "1", tab });
  if (disease) params.set("disease", disease);
  return `/field-mappings?${params}`;
};

/** Router state on that link: the patient page and tab to come back to. */
export interface AddFieldReturn {
  path: string;
  tab: string;
}

/**
 * The return a navigation's state names, or null. Router state is the app's
 * own, but a path is only followed back when it is a patient page.
 */
export function addFieldReturn(state: unknown): AddFieldReturn | null {
  const from = (state as { addFieldReturn?: Partial<AddFieldReturn> } | null)?.addFieldReturn;
  if (!from || typeof from.path !== "string" || typeof from.tab !== "string") return null;
  return /^\/patient\/[^/]+$/.test(from.path) ? { path: from.path, tab: from.tab } : null;
}

/** The tab a patient page was asked to open on, when coming back. */
export function returnedTab(state: unknown): string | null {
  const tab = (state as { tab?: unknown } | null)?.tab;
  return typeof tab === "string" ? tab : null;
}
