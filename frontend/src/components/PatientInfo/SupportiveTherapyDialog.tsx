import { useEffect, useState } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui-labs/dialog';
import { listTherapyRegimens, THERAPY_INTENT_CHOICES, DISCONTINUATION_REASON_CHOICES } from '@/api/therapyLines';
import { saveSupportiveTherapy, type SupportiveTherapyCourse } from '@/api/supportiveTherapies';
import type { TherapyRegimen } from '@/types/therapy';
import { today } from '@/api/clinicalFacts';

interface Props {
  personId: number;
  diseaseCode?: string;
  course?: SupportiveTherapyCourse;
  onClose: () => void;
  onSaved: (record: Record<string, unknown>) => void;
}

export default function SupportiveTherapyDialog({ personId, diseaseCode, course, onClose, onSaved }: Props) {
  const [regimens, setRegimens] = useState<TherapyRegimen[]>([]);
  const [regimen, setRegimen] = useState(course?.regimen_code ?? '');
  const [start, setStart] = useState(course?.start_date ?? '');
  const [end, setEnd] = useState(course?.end_date ?? '');
  const [intent, setIntent] = useState(course?.intent ?? '');
  const [reason, setReason] = useState(course?.discontinuation_reason ?? '');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    listTherapyRegimens(diseaseCode ?? '', 'supportive_therapy').then((items) => {
      if (active) setRegimens(items);
    }).catch(() => {
      if (active) setError('Could not load supportive therapies. Close and reopen to retry.');
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [diseaseCode]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError('');
    try {
      const result = await saveSupportiveTherapy({ person: personId, regimen_code: regimen,
        start_date: start || null, end_date: end || null, intent, discontinuation_reason: reason }, course?.id);
      onSaved(result.patient_info);
      onClose();
    } catch (err: unknown) {
      const data = (err as { response?: { data?: unknown } }).response?.data;
      setError(data ? (typeof data === 'string' ? data : Object.entries(data).map(([key, value]) => `${key}: ${String(value)}`).join('; ')) : 'Could not save supportive therapy. Please try again.');
    } finally { setSaving(false); }
  };
  const inputClass = 'w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm';
  return (
    <Dialog open onOpenChange={(open) => { if (!open && !saving) onClose(); }}>
      <DialogContent aria-describedby={undefined}>
        <DialogHeader><DialogTitle>{course ? 'Edit supportive therapy' : 'Add supportive therapy'}</DialogTitle></DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          <div>
            <label htmlFor="supportive-regimen">Supportive therapy</label>
            <select id="supportive-regimen" className={inputClass} required disabled={loading || saving} value={regimen} onChange={(e) => setRegimen(e.target.value)}>
              <option value="">{loading ? 'Loading therapies…' : 'Select a supportive therapy…'}</option>
              {course && !regimens.some((r) => r.code === course.regimen_code) && <option value={course.regimen_code}>{course.regimen_title}</option>}
              {regimens.map((r) => <option key={r.code} value={r.code}>{r.title}</option>)}
            </select>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div><label htmlFor="supportive-start">Start date</label><input id="supportive-start" type="date" max={today()} className={inputClass} value={start} onChange={(e) => setStart(e.target.value)} /></div>
            <div><label htmlFor="supportive-end">End date</label><input id="supportive-end" type="date" min={start || undefined} className={inputClass} value={end} onChange={(e) => setEnd(e.target.value)} /></div>
          </div>
          <div><label htmlFor="supportive-intent">Therapy intent</label>
            <select id="supportive-intent" className={inputClass} value={intent} onChange={(e) => setIntent(e.target.value)}>
              <option value="">Not recorded</option><option value="Supportive">Supportive</option>
              {intent && intent !== 'Supportive' && !THERAPY_INTENT_CHOICES.some((o) => o.value === intent) && <option>{intent}</option>}
              {THERAPY_INTENT_CHOICES.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </div>
          <div><label htmlFor="supportive-reason">Reason for discontinuation</label>
            <select id="supportive-reason" className={inputClass} value={reason} onChange={(e) => setReason(e.target.value)}>
              <option value="">Not recorded</option>
              {reason && !DISCONTINUATION_REASON_CHOICES.some((o) => o.value === reason) && <option>{reason}</option>}
              {DISCONTINUATION_REASON_CHOICES.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </div>
          {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
          <div className="flex justify-end gap-2">
            <button type="button" disabled={saving} onClick={onClose} className="rounded-md border px-3 py-1.5">Cancel</button>
            <button type="submit" disabled={saving || loading || !regimen} className="rounded-md bg-primary px-3 py-1.5 text-primary-foreground disabled:opacity-50">{saving ? 'Saving…' : 'Save supportive therapy'}</button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
