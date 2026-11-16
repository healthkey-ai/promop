/** The filename a `Content-Disposition: attachment; filename="..."` header names. */
export function filenameFromDisposition(disposition: unknown, fallback: string): string {
  return /filename="([^"]+)"/.exec(String(disposition ?? ""))?.[1] ?? fallback;
}

/** Hand a downloaded blob to the browser to save under `filename`. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/**
 * The message in a failed blob request. With `responseType: "blob"` the error
 * body arrives as a Blob too, so a JSON `detail` has to be read out of it.
 */
export async function blobErrorMessage(error: unknown, fallback: string): Promise<string> {
  const data = (error as { response?: { data?: unknown } })?.response?.data;
  if (!(data instanceof Blob)) return fallback;
  try {
    const parsed = JSON.parse(await data.text()) as { error?: string; detail?: string };
    return parsed.error || parsed.detail || fallback;
  } catch {
    return fallback;
  }
}
