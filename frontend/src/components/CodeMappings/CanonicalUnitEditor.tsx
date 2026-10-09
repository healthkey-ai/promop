import { useEffect, useState } from 'react';
import api from '@/api/axios';

type UnitSetting = {
  unit: string; revision: number; property: string;
  available_units: string[]; example_units: string[]; can_edit: boolean;
};

export default function CanonicalUnitEditor({ conceptId }: { conceptId: number }) {
  const [setting, setSetting] = useState<UnitSetting | null>(null);
  const [unit, setUnit] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  useEffect(() => {
    let active = true;
    api.get<UnitSetting>(`/v1/concepts/${conceptId}/canonical-unit/`).then(({ data }) => {
      if (!Array.isArray(data.available_units) || !Array.isArray(data.example_units)) throw new Error('Invalid unit settings');
      if (active) { setSetting(data); setUnit(data.unit); }
    }).catch(() => { if (active) setError('Could not load unit settings. Reopen this mapping to retry.'); });
    return () => { active = false; };
  }, [conceptId]);
  const save = async () => {
    if (!setting || busy) return;
    setBusy(true); setError(''); setMessage('');
    try {
      const { data } = await api.put<UnitSetting>(`/v1/concepts/${conceptId}/canonical-unit/`, { unit, revision: setting.revision });
      setSetting(data); setUnit(data.unit);
      setMessage('Unit setting saved. Reload lab results to see existing and new results in this unit.');
    } catch (failure) {
      setError((failure as { response?: { data?: { detail?: string } } }).response?.data?.detail || 'Could not save the unit setting.');
    } finally { setBusy(false); }
  };
  return <section aria-label="Canonical measurement unit" className="mt-4 space-y-3 rounded-md border border-slate-200 bg-slate-50 p-4">
    <div><h4 className="text-sm font-semibold text-slate-950">Canonical unit for this instance</h4>
      <p className="mt-1 text-xs text-slate-600">Applies to this LOINC concept across all organizations. Lab results and reference ranges are converted together; the original values and units are preserved.</p></div>
    {error && <p role="alert" className="text-sm text-rose-700">{error}</p>}
    {message && <p role="status" className="text-sm text-green-800">{message}</p>}
    {setting ? <>
      {setting.example_units.length > 0 && <p className="text-xs text-slate-600">LOINC example units: {setting.example_units.join(', ')}</p>}
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm font-medium">Canonical unit
          <select aria-label="Canonical unit" value={unit} disabled={!setting.can_edit || busy}
            onChange={event => { setUnit(event.target.value); setMessage(''); }} className="mt-1 block rounded-md border border-slate-300 bg-white px-3 py-2">
            <option value="">Preserve source units</option>
            {setting.unit && !setting.available_units.includes(setting.unit) && <option value={setting.unit}>{setting.unit} (no longer supported)</option>}
            {setting.available_units.map(value => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
        {setting.can_edit && <button type="button" onClick={() => void save()} disabled={busy || unit === setting.unit}
          className="rounded-md bg-slate-950 px-3 py-2 text-sm font-medium text-white disabled:opacity-40">{busy ? 'Saving…' : 'Save unit setting'}</button>}
      </div>
      {!setting.available_units.length && <p className="text-xs text-amber-800">No validated conversions are available for this measurement property yet.</p>}
      {!setting.can_edit && <p className="text-xs text-slate-600">An instance administrator can change this setting.</p>}
      <p className="text-xs text-slate-500">Saved separately from the mapping. Missing or incompatible source units are flagged. Integration fields with a unit in their name retain that unit.</p>
    </> : !error && <p role="status" className="text-sm text-slate-600">Loading unit settings…</p>}
  </section>;
}
