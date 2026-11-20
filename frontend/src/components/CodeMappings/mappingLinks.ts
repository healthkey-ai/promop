/** The Code Mapping page with one mapping's Edit Mapping dialog open (#2702). */
export function editMappingUrl(mappingId: number): string {
  return `/code-mappings?${new URLSearchParams({ direction: "forward", mapping: String(mappingId) }).toString()}`;
}

/** The mapping id a link made by `editMappingUrl` names, if it names one. */
export function linkedMappingId(params: URLSearchParams): number | undefined {
  const id = Number(params.get("mapping"));
  return Number.isInteger(id) && id > 0 ? id : undefined;
}
