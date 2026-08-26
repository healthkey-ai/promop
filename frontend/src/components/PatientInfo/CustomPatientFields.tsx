import { useCallback, useEffect, useState } from "react";
import { Plus, Search, X } from "lucide-react";
import api from "@/api/axios";

type Mode = "editable" | "computed";

interface CustomField {
  id: number;
  field_name: string;
  display_name: string;
  tab: string;
  field_type: "text" | "number" | "date" | "boolean";
  mode: Mode;
  concept_id: number;
  concept_name: string;
  vocabulary_id: string;
  concept_code: string;
  omop_table: string;
  unit: string;
}

interface Concept {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  vocabulary_id: string;
}

const OMOP_TABLES = [
  "Person", "Location", "CareSite", "Provider", "ObservationPeriod",
  "VisitOccurrence", "ConditionOccurrence", "DrugExposure",
  "ProcedureOccurrence", "Measurement", "Observation", "Death",
  "Specimen", "Note", "NoteNlp",
];

function displayValue(value: unknown, field: CustomField) {
  if (value === undefined || value === null || value === "") return "Not recorded";
  if (field.field_type === "boolean") return value ? "Yes" : "No";
  return String(value) + (field.unit && typeof value === "number" ? ` ${field.unit}` : "");
}

function EditableValue({ field, value, onSave }: { field: CustomField; value: unknown; onSave: (value: unknown) => Promise<void> }) {
  const [draft, setDraft] = useState(value == null ? "" : String(value));
  const [saving, setSaving] = useState(false);
  const save = async (next: unknown) => {
    setSaving(true);
    try { await onSave(next); } finally { setSaving(false); }
  };
  if (field.field_type === "boolean") return <label className="mt-1 flex min-h-9 items-center gap-2 rounded border px-3 text-sm"><input type="checkbox" checked={value === true} disabled={saving} onChange={(e) => void save(e.target.checked)} />{value ? "Yes" : "No"}</label>;
  return <input type={field.field_type === "number" ? "number" : field.field_type === "date" ? "date" : "text"} value={draft} disabled={saving} onChange={(e) => setDraft(e.target.value)} onBlur={() => { if (draft !== String(value ?? "")) void save(draft); }} className="mt-1 h-9 w-full rounded border px-3 text-sm disabled:opacity-60" aria-label={field.display_name} />;
}

export function AddCustomFieldDialog({ tab, onClose, onCreated }: {
  tab: string; onClose: () => void; onCreated: () => void;
}) {
  const [displayName, setDisplayName] = useState("");
  const [fieldName, setFieldName] = useState("");
  const [fieldType, setFieldType] = useState<CustomField["field_type"]>("text");
  const [mode, setMode] = useState<Mode>("editable");
  const [formula, setFormula] = useState("");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Concept[]>([]);
  const [selected, setSelected] = useState<Concept | null>(null);
  const [omopTable, setOmopTable] = useState("Measurement");
  const [unit, setUnit] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const timer = setTimeout(async () => {
      if (query.trim().length < 3) { setResults([]); return; }
      try {
        const response = await api.get("/v1/concepts/search/", { params: { q: query, limit: "20" } });
        setResults(response.data.results || response.data || []);
      } catch { setResults([]); }
    }, 300);
    return () => clearTimeout(timer);
  }, [query]);

  const nameFromLabel = useCallback((label: string) => (
    label.toLowerCase().trim().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "")
  ), []);

  const save = async () => {
    if (!selected || !confirmed) return;
    setSaving(true); setError("");
    try {
      await api.post("/v1/custom-patient-fields/", {
        confirm_patient_record: true,
        display_name: displayName.trim(),
        field_name: fieldName.trim(),
        field_type: fieldType,
        mode,
        formula: mode === "computed" ? formula.trim() : undefined,
        tab,
        concept: selected.concept_id,
        omop_table: omopTable,
        unit,
      });
      onCreated();
      onClose();
    } catch (err: unknown) {
      setError(err && typeof err === "object" && "response" in err
        ? JSON.stringify((err as { response: { data: unknown } }).response.data)
        : "Could not add the field.");
    } finally { setSaving(false); }
  };

  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
    <div className="relative max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-lg border bg-white p-6 shadow-xl">
      <button onClick={onClose} className="absolute right-3 top-3 rounded p-1 text-gray-400 hover:text-gray-700" aria-label="Close"><X size={16} /></button>
      <h2 className="text-lg font-semibold">Add field to PatientRecord</h2>
      <p className="mb-4 text-sm text-gray-500">This field will appear at the bottom of the {tab} tab after its approved mapping is saved.</p>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="text-sm">Display name<input value={displayName} onChange={(e) => { setDisplayName(e.target.value); setFieldName(nameFromLabel(e.target.value)); }} className="mt-1 h-9 w-full rounded border px-2" /></label>
        <label className="text-sm">Field name<input value={fieldName} onChange={(e) => setFieldName(e.target.value)} className="mt-1 h-9 w-full rounded border px-2 font-mono" placeholder="lower_snake_case" /></label>
        <label className="text-sm">Value type<select value={fieldType} onChange={(e) => setFieldType(e.target.value as CustomField["field_type"])} className="mt-1 h-9 w-full rounded border px-2"><option value="text">Text</option><option value="number">Number</option><option value="date">Date</option><option value="boolean">Boolean</option></select></label>
        <fieldset className="text-sm"><legend>Field mode</legend><div className="mt-1 flex gap-4"><label><input type="radio" checked={mode === "editable"} onChange={() => setMode("editable")} /> Editable</label><label><input type="radio" checked={mode === "computed"} onChange={() => setMode("computed")} /> Computed</label></div></fieldset>
      </div>
      {mode === "computed" && <label className="mt-3 block text-sm">Formula<textarea value={formula} onChange={(e) => setFormula(e.target.value)} className="mt-1 w-full rounded border p-2 font-mono" rows={3} placeholder="e.g. weight / (height / 100) ^ 2" /></label>}
      <div className="mt-4 border-t pt-4"><p className="mb-2 text-sm font-medium">Concept mapping</p>
        {selected ? <div className="flex items-center gap-2 rounded border bg-blue-50 p-2 text-sm"><span className="font-mono">{selected.vocabulary_id}:{selected.concept_code}</span><span>{selected.concept_name}</span><button className="ml-auto" onClick={() => setSelected(null)}><X size={14} /></button></div> : <><div className="relative"><Search size={14} className="absolute left-2 top-2.5 text-gray-400" /><input value={query} onChange={(e) => setQuery(e.target.value)} className="h-9 w-full rounded border pl-8 pr-2" placeholder="Search concepts..." /></div>{results.length > 0 && <div className="max-h-36 overflow-y-auto border"><table className="w-full text-sm"><tbody>{results.map((concept) => <tr key={concept.concept_id} className="cursor-pointer hover:bg-blue-50" onClick={() => { setSelected(concept); setResults([]); }}><td className="p-2 font-mono">{concept.vocabulary_id}:{concept.concept_code}</td><td className="p-2">{concept.concept_name}</td></tr>)}</tbody></table></div>}</>}
        <div className="mt-3 grid gap-3 sm:grid-cols-2"><label className="text-sm">OMOP table<select value={omopTable} onChange={(e) => setOmopTable(e.target.value)} className="mt-1 h-9 w-full rounded border px-2">{OMOP_TABLES.map((table) => <option key={table}>{table}</option>)}</select></label><label className="text-sm">Unit (optional)<input value={unit} onChange={(e) => setUnit(e.target.value)} className="mt-1 h-9 w-full rounded border px-2" /></label></div>
      </div>
      <label className="mt-4 flex items-start gap-2 text-sm"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} className="mt-0.5" />I confirm that I want to add this field to PatientRecord.</label>
      {error && <p className="mt-3 text-sm text-red-600">{error}</p>}
      <div className="mt-5 flex justify-end gap-2"><button onClick={onClose} className="rounded border px-4 py-2 text-sm">Cancel</button><button onClick={save} disabled={!displayName.trim() || !fieldName.trim() || !selected || !confirmed || (mode === "computed" && !formula.trim()) || saving} className="rounded bg-primary px-4 py-2 text-sm text-primary-foreground disabled:opacity-50">{saving ? "Adding..." : "Add field"}</button></div>
    </div>
  </div>;
}

export function CustomPatientFields({ tab, formData, canManage, onEditableValueChange }: { tab: string; formData: Record<string, unknown>; canManage?: boolean; onEditableValueChange?: (field: CustomField, value: unknown) => Promise<void> }) {
  const [fields, setFields] = useState<CustomField[]>([]);
  const [showDialog, setShowDialog] = useState(false);
  const load = useCallback(async () => {
    try { const response = await api.get("/v1/custom-patient-fields/"); setFields(response.data); } catch { setFields([]); }
  }, []);
  useEffect(() => {
    const timer = setTimeout(() => { void load(); }, 0);
    return () => clearTimeout(timer);
  }, [load]);
  const visible = fields.filter((field) => field.tab === tab);
  const values = (formData.custom_fields as Record<string, unknown> | undefined) || {};
  return <section className="mt-8 border-t border-border pt-6" data-testid="custom-patient-fields">
    <div className="mb-4 flex items-center justify-between"><div><h3 className="text-sm font-semibold">Additional fields</h3><p className="text-xs text-muted-foreground">Approved fields configured for this tab.</p></div>{canManage && <button onClick={() => setShowDialog(true)} className="inline-flex items-center gap-1 rounded border px-2.5 py-1.5 text-xs font-medium hover:bg-muted"><Plus size={14} /> Add field</button>}</div>
    {visible.length > 0 && <div className="grid gap-4 sm:grid-cols-2">{visible.map((field) => <div key={field.id}><div className="flex items-center gap-1 text-xs font-medium text-muted-foreground"><span>{field.display_name}</span><span className="rounded bg-muted px-1.5 py-0.5 capitalize">{field.mode}</span></div>{field.mode === "editable" && onEditableValueChange ? <EditableValue key={`${field.id}:${String(values[field.field_name] ?? "")}`} field={field} value={values[field.field_name]} onSave={(value) => onEditableValueChange(field, value)} /> : <div className="mt-1 min-h-9 rounded border bg-muted/30 px-3 py-2 text-sm">{displayValue(values[field.field_name], field)}</div>}</div>)}</div>}
    {visible.length === 0 && <p className="text-sm text-muted-foreground">No additional fields configured.</p>}
    {showDialog && <AddCustomFieldDialog tab={tab} onClose={() => setShowDialog(false)} onCreated={load} />}
  </section>;
}
