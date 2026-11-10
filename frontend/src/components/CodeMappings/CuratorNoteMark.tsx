import { StickyNote } from "lucide-react";

/** A yellow sticky note on a source code a curator has left a note on; the
 * note shows on hover. Suggest's rationale lives in the same field and is not
 * marked: the server says which notes are a curator's (#2423). */
export function CuratorNoteMark({ note }: { note?: string | null }) {
  const text = (note ?? "").trim();
  if (!text) return null;
  return (
    <span role="img" aria-label={`Curator note: ${text}`} title={text} data-testid="curator-note"
      className="ml-1 inline-flex align-middle text-amber-600">
      <StickyNote size={14} aria-hidden="true" className="fill-yellow-200" />
    </span>
  );
}
