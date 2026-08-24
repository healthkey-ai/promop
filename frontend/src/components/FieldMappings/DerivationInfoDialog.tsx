import { X } from "lucide-react";

interface Provenance {
  omop_table: string;
  lookup_strategy: string;
  concept_codes: string[] | null;
  source_values: string[] | null;
  extractor: string;
  selection_rule: string;
  description: string;
}

interface Props {
  fieldName: string;
  provenance: Provenance | null;
  onClose: () => void;
}

/** Read-only explanation for application-owned field derivations. */
export function DerivationInfoDialog({ fieldName, provenance, onClose }: Props) {
  return (
    <div
      onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
    >
      <div className="relative w-full max-w-lg rounded-lg border bg-white p-6 shadow-xl">
        <button
          onClick={onClose}
          className="absolute right-3 top-3 rounded p-1 text-gray-400 hover:text-gray-600"
          aria-label="Close derivation details"
        >
          <X size={16} />
        </button>
        <h2 className="mb-1 text-lg font-semibold">Derivation details</h2>
        <p className="mb-4 text-sm text-gray-500">
          Field: <span className="font-mono">{fieldName}</span>
        </p>

        {provenance ? (
          <div className="space-y-3 text-sm">
            <p className="rounded bg-gray-50 p-3 text-gray-700">{provenance.description}</p>
            <dl className="grid grid-cols-[10rem_1fr] gap-x-3 gap-y-2">
              <dt className="text-gray-500">OMOP table</dt>
              <dd>{provenance.omop_table}</dd>
              <dt className="text-gray-500">Concept hierarchy</dt>
              <dd>{provenance.concept_codes?.length ? provenance.concept_codes.join(", ") : "None"}</dd>
              <dt className="text-gray-500">Lookup</dt>
              <dd>{provenance.lookup_strategy}</dd>
              <dt className="text-gray-500">Selection rule</dt>
              <dd>{provenance.selection_rule}</dd>
              <dt className="text-gray-500">Application extractor</dt>
              <dd className="font-mono text-xs">{provenance.extractor}</dd>
            </dl>
          </div>
        ) : (
          <p className="rounded bg-gray-50 p-3 text-sm text-gray-600">
            This field is derived by application code. No standalone concept mapping can be edited.
          </p>
        )}

        <p className="mt-5 text-xs text-gray-400">
          Read-only: changing a concept mapping would not change this derivation.
        </p>
      </div>
    </div>
  );
}
