import { useEffect, useState } from 'react';
import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';
import { useVocabulary } from '@/hooks/useVocabulary';
import { usePatientFormStore } from '@/federation/PatientFormContext';
import { Button } from '@/components/shadcn/button';

export interface ConditionsAnswer {
  no_pre_existing_conditions: boolean | null;
  preexisting_conditions: string[] | null;
  /** The generic "adopt these server values" channel (#2606) also carries an
   * explicit answer's field, so the payload is not closed to these two keys. */
  [field: string]: unknown;
}

export default function PreexistingConditionsAnswer({
  personId, flag, categories, readOnly, onSaved,
}: {
  personId: number;
  flag: unknown;
  categories: unknown;
  readOnly: boolean;
  onSaved: (answer: ConditionsAnswer) => void | Promise<void>;
}) {
  const store = usePatientFormStore();
  const { options, loading } = useVocabulary('pre-existing-condition-category', 'code');
  const [selected, setSelected] = useState<string[]>(
    Array.isArray(categories) ? categories.filter((v): v is string => typeof v === 'string') : []);
  const [busy, setBusy] = useState(false);
  // The list is a multi-value choice, not an always-open checklist: the row
  // shows what is selected and opens the categories on request (#2607). A
  // stored answer starts open so the record never hides what it holds.
  const [chooserOpen, setChooserOpen] = useState(
    Array.isArray(categories) && categories.length > 0,
  );
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [loaded, setLoaded] = useState<ConditionsAnswer | null>(null);
  const needsSnapshot = flag === undefined;
  const answerFlag = needsSnapshot ? loaded?.no_pre_existing_conditions : flag;
  const answerCategories = needsSnapshot ? loaded?.preexisting_conditions : categories;
  const ready = !needsSnapshot || loaded !== null;
  // A saved answer comes back as new props — or, on a host that withholds the
  // flag, as the snapshot this component fetched. Re-seed from the effective
  // answer during render instead of remounting on a key: a remount also dropped
  // the "Answer saved." confirmation the save had just set. The disclosure opens
  // with stored categories so the record never hides what it holds (#2607).
  const answerKey = JSON.stringify([answerFlag ?? null, answerCategories ?? null]);
  const [seededFrom, setSeededFrom] = useState(answerKey);
  if (seededFrom !== answerKey) {
    setSeededFrom(answerKey);
    const stored = Array.isArray(answerCategories)
      ? answerCategories.filter((v): v is string => typeof v === 'string') : [];
    setSelected(stored);
    if (stored.length > 0) setChooserOpen(true);
    // An answer that changed outside this editor answers its old alert ("This
    // answer changed. Reload…") and outdates its old confirmation. Our own
    // save is still busy when its result arrives, and sets its message after.
    if (!busy) {
      setError('');
      setMessage('');
    }
  }
  useEffect(() => {
    if (!needsSnapshot) return;
    let cancelled = false;
    clinicalClient().get<ConditionsAnswer>(clinicalUrl(
      `/v1/patient-records/${personId}/preexisting-conditions-answer/`,
    )).then(({ data }) => {
      if (cancelled) return;
      setLoaded(data);
      setSelected(Array.isArray(data.preexisting_conditions) ? data.preexisting_conditions : []);
    }).catch(() => {
      if (!cancelled) setError('Could not load the current answer. Reload before confirming.');
    });
    return () => { cancelled = true; };
  }, [needsSnapshot, personId]);
  const hasCategories = answerCategories != null && JSON.stringify(answerCategories) !== '[]';
  const malformed = answerCategories != null && !Array.isArray(answerCategories);
  const unknownCodes = selected.filter(code => !options.some(option => option.value === code));
  const labelFor = (code: string) => options.find(option => option.value === code)?.label || code;
  // Every counted code is named, including one the vocabulary does not carry and
  // one whose label has not loaded yet (#2607 review).
  const selectedLabels = selected.map(labelFor);
  const recordedCodes = Array.isArray(answerCategories)
    ? answerCategories.filter((v): v is string => typeof v === 'string') : [];
  const recordedLabels = recordedCodes.map(labelFor);
  const pendingDiffers = JSON.stringify([...selected].sort()) !== JSON.stringify([...recordedCodes].sort());
  const locked = readOnly || busy || !ready;

  const save = async (action: 'confirm_none' | 'withdraw' | 'set_categories') => {
    setBusy(true);
    setError('');
    setMessage('');
    // The attestation endpoint bypasses the save queue; hold the host's
    // listeners until the answer, its acknowledgement and `onSaved` land.
    store?.beginBypassWrite();
    try {
      const { data } = await clinicalClient().post<ConditionsAnswer>(
        clinicalUrl(`/v1/patient-records/${personId}/preexisting-conditions-answer/`), {
          action,
          expected_flag: answerFlag ?? null,
          expected_categories: answerCategories ?? null,
          ...(action === 'set_categories' ? { categories: selected } : {}),
        });
      await onSaved(data);
      setMessage(action === 'confirm_none' ? 'Answer explicitly confirmed.' : 'Answer saved.');
    } catch (e) {
      const status = (e as { response?: { status?: number } }).response?.status;
      setError(status === 409 ? 'This answer changed. Reload before confirming.'
        : 'Could not verify whether the answer was saved. Reload before retrying.');
      store?.failBypassWrite('Could not save the pre-existing conditions answer.');
    } finally {
      store?.endBypassWrite();
      setBusy(false);
    }
  };

  return <div className="space-y-3">
    <p className="text-sm font-medium">Pre-existing Conditions</p>
    <p className="text-sm">
      {!ready ? 'Loading the current answer…' : hasCategories ? 'Recorded categories are listed below.' : answerFlag === true
        ? 'Stored answer: no pre-existing conditions. Use Confirm to explicitly reaffirm it.'
        : answerFlag === false ? 'Stored answer: conditions reported; categories not yet recorded.'
        : 'Not answered. An empty list does not mean no conditions.'}
    </p>
    {malformed && <p role="alert">Categories need review. Confirmation is unavailable.</p>}
    {!malformed && <>
      {/* Reading the list is not an edit: the disclosure stays usable for a
          read-only caller, while the checkboxes inside are disabled. */}
      <Button type="button" variant="outline" size="sm" aria-expanded={chooserOpen}
        disabled={busy || !ready}
        onClick={() => setChooserOpen((open) => !open)}>
        {selected.length
          ? `Choose conditions (${selected.length} selected)`
          : 'Choose conditions'}
        <span aria-hidden>{chooserOpen ? '▴' : '▾'}</span>
      </Button>
      {selectedLabels.length > 0 && <p className="text-sm">Selected: {selectedLabels.join(', ')}</p>}
      {pendingDiffers && recordedLabels.length > 0 && (
        <p className="text-sm text-muted-foreground">Recorded: {recordedLabels.join(', ')}</p>
      )}
      {chooserOpen && <div role="group" aria-label="Pre-existing condition categories"
        className="space-y-1 rounded-md border border-input p-3">
        {loading ? <p>Loading categories…</p> : options.map(option => <label
          key={option.value} className="flex items-center gap-2 text-sm">
          <input type="checkbox" disabled={locked} checked={selected.includes(option.value)} onChange={e => {
            setSelected(e.target.checked ? [...selected, option.value]
              : selected.filter(code => code !== option.value));
          }} />{option.label}
        </label>)}
      </div>}
      {unknownCodes.length > 0 && <p role="alert">Some recorded category codes need review.</p>}
      <Button type="button" variant="outline" size="sm"
        disabled={locked || loading || !options.length || unknownCodes.length > 0}
        onClick={() => save('set_categories')}>Save categories and withdraw “none”</Button>
    </>}
    <p className="text-xs text-muted-foreground">These answers save only when you press a button, not through autosave.</p>
    <div className="flex flex-wrap gap-2">
      <Button type="button" variant="outline" size="sm"
        disabled={locked || hasCategories || selected.length > 0}
        onClick={() => save('confirm_none')}>Confirm no pre-existing conditions</Button>
      <Button type="button" variant="outline" size="sm"
        disabled={locked}
        onClick={() => save('withdraw')}>Withdraw answer</Button>
    </div>
    {busy && <p role="status">Saving answer…</p>}
    {message && <p role="status">{message}</p>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
