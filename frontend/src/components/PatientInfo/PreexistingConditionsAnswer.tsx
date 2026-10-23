import { useEffect, useState } from 'react';
import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';
import { useVocabulary } from '@/hooks/useVocabulary';

export interface ConditionsAnswer {
  no_pre_existing_conditions: boolean | null;
  preexisting_conditions: string[] | null;
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
  const { options, loading } = useVocabulary('pre-existing-condition-category', 'code');
  const [selected, setSelected] = useState<string[]>(
    Array.isArray(categories) ? categories.filter((v): v is string => typeof v === 'string') : []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [loaded, setLoaded] = useState<ConditionsAnswer | null>(null);
  const needsSnapshot = flag === undefined;
  const answerFlag = needsSnapshot ? loaded?.no_pre_existing_conditions : flag;
  const answerCategories = needsSnapshot ? loaded?.preexisting_conditions : categories;
  const ready = !needsSnapshot || loaded !== null;
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

  const save = async (action: 'confirm_none' | 'withdraw' | 'set_categories') => {
    setBusy(true);
    setError('');
    setMessage('');
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
    } finally {
      setBusy(false);
    }
  };

  return <fieldset disabled={readOnly || busy || !ready} className="space-y-3">
    <legend className="text-sm font-medium">Pre-existing Conditions</legend>
    <p className="text-sm">
      {!ready ? 'Loading the current answer…' : hasCategories ? 'Recorded categories are listed below.' : answerFlag === true
        ? 'Stored answer: no pre-existing conditions. Use Confirm to explicitly reaffirm it.'
        : answerFlag === false ? 'Stored answer: conditions reported; categories not yet recorded.'
        : 'Not answered. An empty list does not mean no conditions.'}
    </p>
    {malformed && <p role="alert">Categories need review. Confirmation is unavailable.</p>}
    {!malformed && <>
      {loading ? <p>Loading categories…</p> : options.map(option => <label
        key={option.value} className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={selected.includes(option.value)} onChange={e => {
          setSelected(e.target.checked ? [...selected, option.value]
            : selected.filter(code => code !== option.value));
        }} />{option.label}
      </label>)}
      {unknownCodes.length > 0 && <p role="alert">Some recorded category codes need review.</p>}
      <button type="button" className="text-sm underline" disabled={loading || !options.length || unknownCodes.length > 0}
        onClick={() => save('set_categories')}>Save categories and withdraw “none”</button>
    </>}
    <p className="text-xs text-muted-foreground">These answers save only when you press a button, not through autosave.</p>
    <div className="flex flex-wrap gap-4">
      <button type="button" className="text-sm underline" disabled={hasCategories || selected.length > 0}
        onClick={() => save('confirm_none')}>Confirm no pre-existing conditions</button>
      <button type="button" className="text-sm underline" onClick={() => save('withdraw')}>Withdraw answer</button>
    </div>
    {busy && <p role="status">Saving answer…</p>}
    {message && <p role="status">{message}</p>}
    {error && <p role="alert">{error}</p>}
  </fieldset>;
}
