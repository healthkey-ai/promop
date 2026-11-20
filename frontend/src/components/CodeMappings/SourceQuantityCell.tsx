/** Qty and Units share a second line for the source FHIR order references. */
export default function SourceQuantityCell({ quantity, units, basedOn = [], correctedUnit = false }: {
  quantity?: string;
  units?: string;
  basedOn?: string[];
  correctedUnit?: boolean;
}) {
  return (
    <td colSpan={2} className="px-3 py-2 text-xs text-slate-700">
      <div className="grid grid-cols-2 gap-x-6 gap-y-1">
        <div className="font-mono">{quantity || "—"}</div>
        <div className="font-mono">
          {units || "—"}
          {correctedUnit && <span className="ml-1 text-blue-700">(corrected)</span>}
        </div>
        {basedOn.length > 0 && <div className="col-span-2 break-words text-slate-500">
          <span className="font-medium">BasedOn: </span>{basedOn.join("; ")}
        </div>}
      </div>
    </td>
  );
}
