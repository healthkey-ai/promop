export function formatUploadError(caught: unknown) {
  const response = caught && typeof caught === "object" && "response" in caught
    ? (caught as { response?: { data?: { detail?: string; errors?: Array<{ row?: number; detail?: string }> } } }).response
    : undefined;
  const first = response?.data?.errors?.[0];
  return first
    ? `${response?.data?.detail || "Upload failed"} Row ${first.row}: ${first.detail}`
    : response?.data?.detail || "Upload failed. Check the CSV and try again.";
}
