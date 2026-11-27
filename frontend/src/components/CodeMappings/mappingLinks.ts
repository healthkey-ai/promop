/** The Code Mapping page with one mapping's Edit Mapping dialog open (#2702). */
export function editMappingUrl(mappingId: number): string {
  return `/code-mappings?${new URLSearchParams({ direction: "forward", mapping: String(mappingId) }).toString()}`;
}

/** The mapping id a link made by `editMappingUrl` names, if it names one. */
export function linkedMappingId(params: URLSearchParams): number | undefined {
  const id = Number(params.get("mapping"));
  return Number.isInteger(id) && id > 0 ? id : undefined;
}

/** The Code Mapping page's Wearables tab narrowed to one metric's source codes
 *  (every device's code for it), from a 30-day value on a patient's Wearables tab. */
export function wearableMetricMappingsUrl(metricKey: string): string {
  return `/code-mappings?${new URLSearchParams({ direction: "forward", metric: metricKey }).toString()}`;
}

/** The metric key a link made by `wearableMetricMappingsUrl` names, if it names one. */
export function linkedMetricKey(params: URLSearchParams): string | undefined {
  const key = params.get("metric") ?? "";
  return /^[a-z][a-z0-9_]{0,63}$/.test(key) ? key : undefined;
}
