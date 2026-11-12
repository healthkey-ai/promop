import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import type { AxiosInstance } from 'axios';
import { Pencil, X } from 'lucide-react';

import { useEditorSession } from './editorSession';
import GenomicsEditor from './GenomicsEditor';
import PatientFieldEditorSlot from './PatientFieldEditorSlot';
import TherapyLinesEditor from './TherapyLinesEditor';
import { isGenomicsTrialRow, isTherapyTrialRow } from './trialRows';
import { usePromopFieldBindings } from './usePromopFieldBindings';

/** What a host supplies once, for every trial-detail editor. */
export interface TrialEditorsConnection {
  apiClient: AxiosInstance;
  apiBasePath?: string;
  connectionKey?: string;
}

/** The three slots EXACT's `TrialMatches` accepts. */
export interface TrialEditorSlots {
  renderFieldEditor: (props: {
    field: string;
    label: string;
    trialId?: string | number;
  }) => ReactNode;
  fieldEditorOwnsValue: (props: {
    field: string;
    label: string;
    trialId?: string | number;
  }) => boolean;
  renderGenomicsEditor: (props: {
    label: string;
    trialId?: string | number;
    onClose: () => void;
  }) => ReactNode;
}

export interface TrialEditorsProps extends TrialEditorsConnection {
  /** Ask the descriptor only when a detail row can exist. */
  enabled?: boolean;
  /** The host's write gate. Read-only still shows values and recorded lines. */
  readOnly?: boolean;
  /** A record write landed (field, therapy line or genomics finding). The host
   *  re-reads its patient payload and invalidates the trial queries. */
  onChanged?: () => void;
  children: (slots: TrialEditorSlots) => ReactNode;
}

/** The therapy-line editor as a row control: a pencil and a non-modal dialog.
 *
 * Non-modal on purpose: the line editor is a Radix dialog of its own, portaled
 * to the body. A modal host would leave pointer-events off and trap focus
 * inside this content, making the nested editor unusable. */
function TherapyRowEditor({
  connection,
  readOnly,
  label,
  open,
  onOpenChange,
  onChanged,
}: {
  connection: TrialEditorsConnection;
  readOnly: boolean;
  label: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChanged?: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = previous; };
  }, [open]);

  return (
    <Dialog.Root modal={false} open={open} onOpenChange={onOpenChange}>
      <span className="inline-flex items-center">
        {/* A real trigger, so Radix returns focus here when the panel closes. */}
        <Dialog.Trigger asChild>
          <button
            type="button"
            aria-label={`Edit ${label}`}
            title={`Edit ${label}`}
            className="rounded p-1 text-muted-foreground hover:text-foreground"
          >
            <Pencil className="h-4 w-4" strokeWidth={1.67} />
          </button>
        </Dialog.Trigger>
      </span>
      <Dialog.Portal>
          {/* Below the nested line editor (z-50): that dialog portals to the
              body and must sit above this panel, not behind it. Centred by
              auto margins, not translate utilities: a Tailwind 3 host sets
              `transform` for those while ours sets `translate`, and both
              moves apply (see ui-labs/dialog.tsx). */}
          <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40" />
          <Dialog.Content
            className="promop-root fixed inset-0 z-[41] m-auto h-fit max-h-[85vh] w-[min(42rem,calc(100%-2rem))] space-y-4 overflow-y-auto rounded-xl bg-background p-6 shadow-xl"
            onInteractOutside={(event) => event.preventDefault()}
            onEscapeKeyDown={(event) => event.preventDefault()}
          >
            <div className="flex items-start justify-between gap-3">
              <Dialog.Title className="font-semibold">{label}</Dialog.Title>
              {/* Escape and outside clicks are the nested editor's; without a
                  close control this panel would have no way out. */}
              <Dialog.Close asChild>
                <button
                  type="button"
                  aria-label="Close therapy editor"
                  className="rounded p-1 text-muted-foreground hover:text-foreground"
                >
                  <X className="h-4 w-4" />
                </button>
              </Dialog.Close>
            </div>
            <Dialog.Description className="text-sm text-muted-foreground">
              Recorded therapy lines. Adding or changing a line updates this
              value automatically.
            </Dialog.Description>
            {open ? (
              <TherapyLinesEditor
                apiClient={connection.apiClient}
                apiBasePath={connection.apiBasePath}
                connectionKey={connection.connectionKey}
                readOnly={readOnly}
                onChanged={onChanged}
              />
            ) : null}
          </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/**
 * One adapter for every host that shares this React tree.
 *
 * EXACT's `TrialMatches` takes three per-row slots. Both hosts (CB `ui.v2` and
 * ht-phr, now on the same React major) would otherwise reimplement the routing:
 * which names are therapy, which open genomics, which scalars the descriptor
 * resolves, and which callback invalidates what. This component answers all
 * three from one descriptor read and hands the slots to a render prop.
 *
 * Mount it only after the remote module loaded: the hook is called
 * unconditionally inside, which a feature-detect cannot do.
 */
export default function TrialEditors({
  apiClient,
  apiBasePath,
  connectionKey,
  enabled = true,
  readOnly = false,
  onChanged,
  children,
}: TrialEditorsProps) {
  const bindings = usePromopFieldBindings(
    { apiClient, apiBasePath, connectionKey },
    { enabled },
  );
  const connection: TrialEditorsConnection = { apiClient, apiBasePath, connectionKey };
  // One therapy panel at a time. `modal={false}` leaves the page interactive,
  // so two rows could otherwise both hold the body-scroll lock and the second
  // close would restore "hidden" from the first.
  const [openTherapy, setOpenTherapy] = useState<string | null>(null);

  // One notification for a scalar save, not one per mounted row. Every scalar
  // slot shares this store, so a listener inside each slot would fire N times
  // for one acknowledged batch. Therapy and genomics mutations bypass the store
  // and keep their own `onChanged`.
  const store = useEditorSession({ apiClient, apiBasePath, connectionKey });
  const changed = useRef(onChanged);
  useEffect(() => { changed.current = onChanged; }, [onChanged]);
  const listener = useMemo(() => () => { changed.current?.(); }, []);
  useEffect(() => store.onUpdated(listener), [store, listener]);

  // Whether `renderFieldEditor` actually draws a scalar slot for this row.
  // `fieldEditorOwnsValue` must answer exactly this: claiming a row whose
  // editor renders null (an unresolved binding, an attestation, a negated
  // yes/no, genomics) suppressed EXACT's own value for a row nobody painted
  // (#2433).
  const scalarSlotRendered = (field: string) => {
    // Therapy rows get a pencil only, and genomics its own dialog.
    if (isTherapyTrialRow(field) || isGenomicsTrialRow(field)) return false;
    if (!bindings.ready || !bindings.canResolve(field)) return false;
    // Attestations and inverted yes/no rows carry their own dialog, snapshot
    // and polarity; EXACT wires those itself (#750, #5663). A field slot here
    // would draw a read-only note or a plain select and suppress that door.
    const kind = bindings.entryFor(field)?.kind;
    return kind !== 'explicit-answer' && kind !== 'negated';
  };

  const slots: TrialEditorSlots = {
    renderFieldEditor: ({ field, label }) => {
      if (isTherapyTrialRow(field)) {
        return (
          <TherapyRowEditor
            key={`therapy:${field}`}
            connection={connection}
            readOnly={readOnly}
            label={label}
            open={openTherapy === field}
            onOpenChange={(next) => setOpenTherapy(next ? field : null)}
            onChanged={onChanged}
          />
        );
      }
      if (!scalarSlotRendered(field)) return null;
      return (
        <PatientFieldEditorSlot
          key={`field:${field}`}
          apiClient={apiClient}
          apiBasePath={apiBasePath}
          connectionKey={connectionKey}
          name={field}
          label={label}
          readOnly={readOnly}
          compact
        />
      );
    },
    // Only the scalar slot paints the value. The therapy pencil and the rows
    // EXACT keeps for itself must leave its own rendering in place.
    fieldEditorOwnsValue: ({ field }) => scalarSlotRendered(field),
    renderGenomicsEditor: ({ onClose }) => (
      <GenomicsEditor
        apiClient={apiClient}
        apiBasePath={apiBasePath}
        connectionKey={connectionKey}
        readOnly={readOnly}
        onClose={onClose}
        onChanged={onChanged}
      />
    ),
  };

  return <>{children(slots)}</>;
}
