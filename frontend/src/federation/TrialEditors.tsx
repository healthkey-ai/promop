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
    <span className="inline-flex items-center">
      <button
        type="button"
        aria-label={`Edit ${label}`}
        title={`Edit ${label}`}
        onClick={() => onOpenChange(true)}
        className="rounded p-1 text-muted-foreground hover:text-foreground"
      >
        <Pencil className="h-4 w-4" strokeWidth={1.67} />
      </button>
      <Dialog.Root modal={false} open={open} onOpenChange={onOpenChange}>
        <Dialog.Portal>
          {/* Below the nested line editor (z-50): that dialog portals to the
              body and must sit above this panel, not behind it. */}
          <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40" />
          <Dialog.Content
            className="promop-root fixed left-1/2 top-1/2 z-[41] max-h-[85vh] w-[min(42rem,calc(100%-2rem))] -translate-x-1/2 -translate-y-1/2 space-y-4 overflow-y-auto rounded-xl bg-background p-6 shadow-xl"
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
    </span>
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
      // Genomics has its own dialog, opened by `renderGenomicsEditor`.
      if (isGenomicsTrialRow(field)) return null;
      if (!bindings.ready || !bindings.canResolve(field)) return null;
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
    // The scalar slot draws the value too; the therapy slot is only a pencil,
    // so EXACT keeps painting the value on those rows.
    fieldEditorOwnsValue: ({ field }) =>
      !isTherapyTrialRow(field) && !isGenomicsTrialRow(field),
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
