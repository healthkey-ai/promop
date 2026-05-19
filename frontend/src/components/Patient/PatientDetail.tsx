import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, Check, AlertCircle } from 'lucide-react';
import api from '../../api/axios';
import { getActiveBranding } from '../../config/branding';
import GeneralTab from '../PatientInfo/tabs/GeneralTab';
import DiseaseTab from '../PatientInfo/tabs/DiseaseTab';
import TreatmentTab from '../PatientInfo/tabs/TreatmentTab';
import BloodTab from '../PatientInfo/tabs/BloodTab';
import LabsTab from '../PatientInfo/tabs/LabsTab';
import BehaviorTab from '../PatientInfo/tabs/BehaviorTab';

// ── Types ────────────────────────────────────────────────────────────────────

type SaveStatus = 'idle' | 'pending' | 'saving' | 'saved' | 'error';

// ── Helper components ────────────────────────────────────────────────────────

function getInitials(name: string) {
  return name.split(' ').filter(Boolean).map(n => n[0]).slice(0, 2).join('').toUpperCase() || '?';
}

function getAvatarBg(name: string) {
  const palette = ['#6366f1', '#8b5cf6', '#0ea5e9', '#10b981', '#f59e0b', '#ec4899'];
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) & 0xffff;
  return palette[h % palette.length];
}


function SaveStatusIndicator({ status, onRetry }: { status: SaveStatus; onRetry: () => void }) {
  if (status === 'idle') return null;
  return (
    <div className="flex items-center gap-1.5 select-none animate-fade-in">
      {status === 'pending' && (
        <span className="text-[11px] text-portal-text-tertiary">Unsaved…</span>
      )}
      {status === 'saving' && (
        <>
          <span className="h-3 w-3 rounded-full border-2 border-portal-text-tertiary border-t-transparent animate-spin inline-block" />
          <span className="text-[11px] text-portal-text-secondary">Saving…</span>
        </>
      )}
      {status === 'saved' && (
        <>
          <Check className="h-3.5 w-3.5 text-emerald-500" strokeWidth={2.5} />
          <span className="text-[11px] font-semibold text-emerald-600">Saved</span>
        </>
      )}
      {status === 'error' && (
        <>
          <AlertCircle className="h-3.5 w-3.5 text-red-500" />
          <button onClick={onRetry} className="text-[11px] font-semibold text-red-600 hover:underline">
            Save failed · Retry
          </button>
        </>
      )}
    </div>
  );
}

// ── Skeleton loader ──────────────────────────────────────────────────────────

function SkeletonField() {
  return (
    <div className="space-y-1.5">
      <div className="h-3.5 w-20 bg-gray-100 rounded animate-pulse" />
      <div className="h-9 w-full bg-gray-100 rounded-lg animate-pulse" />
    </div>
  );
}

function PatientDetailSkeleton() {
  return (
    <div className="min-h-screen bg-[#f5f7fa]">
      <div className="bg-white/90 border-b border-portal-border h-14 flex items-center">
        <div className="max-w-5xl mx-auto w-full px-6 flex items-center gap-3">
          <div className="h-8 w-8 bg-gray-100 rounded-full animate-pulse" />
          <div className="h-8 w-8 bg-gray-100 rounded-full animate-pulse" />
          <div className="h-4 w-36 bg-gray-100 rounded animate-pulse" />
          <div className="h-5 w-10 bg-gray-100 rounded-full animate-pulse ml-1" />
        </div>
      </div>
      <div className="max-w-5xl mx-auto px-6 py-6">
        <div className="flex gap-6 pb-3 border-b border-portal-border mb-5">
          {['w-14', 'w-28', 'w-20', 'w-14', 'w-10', 'w-18'].map((w, i) => (
            <div key={i} className={`h-4 ${w} bg-gray-100 rounded animate-pulse`} />
          ))}
        </div>
        <div className="rounded-2xl bg-white shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)] px-8 py-8 space-y-10">
          <div>
            <div className="h-6 w-20 bg-gray-100 rounded animate-pulse mb-2" />
            <div className="h-3.5 w-64 bg-gray-100 rounded animate-pulse mb-7" />
            <div className="grid grid-cols-2 gap-x-8 gap-y-5">
              {Array.from({ length: 6 }).map((_, i) => <SkeletonField key={i} />)}
            </div>
          </div>
          <div className="pt-8 border-t border-portal-border">
            <div className="h-5 w-28 bg-gray-100 rounded animate-pulse mb-2" />
            <div className="h-3.5 w-44 bg-gray-100 rounded animate-pulse mb-7" />
            <div className="grid grid-cols-2 gap-x-8 gap-y-5">
              {Array.from({ length: 4 }).map((_, i) => <SkeletonField key={i} />)}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Main component ───────────────────────────────────────────────────────────

const PatientDetail: React.FC = () => {
  const { personId } = useParams<{ personId: string }>();
  const navigate     = useNavigate();

  const [loading, setLoading]         = useState(true);
  const [fetchError, setFetchError]   = useState<string | null>(null);
  const [saveStatus, setSaveStatus]   = useState<SaveStatus>('idle');
  const [patientInfo, setPatientInfo] = useState<any>(null);
  const [editedInfo, setEditedInfo]   = useState<any>({});
  const [patientName, setPatientName] = useState('');
  const [editedName, setEditedName]   = useState('');
  const [activeTab, setActiveTab]     = useState(0);

  const autoSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveSeqRef       = useRef(0);
  const pendingDataRef   = useRef<{ info: any; name: string } | null>(null);
  const editedInfoRef    = useRef<any>({});
  const editedNameRef    = useRef('');
  const patientNameRef   = useRef('');

  useEffect(() => { editedInfoRef.current  = editedInfo;  }, [editedInfo]);
  useEffect(() => { editedNameRef.current  = editedName;  }, [editedName]);
  useEffect(() => { patientNameRef.current = patientName; }, [patientName]);
  useEffect(() => () => { if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current); }, []);

  // ── Fetch ─────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!personId) return;
    (async () => {
      try {
        setLoading(true);
        const res = await api.get(`/patient-info/${personId}/`);
        const d   = res.data.patient_info;

        if (d.ecog_performance_status != null)
          d.ecog_performance_status = String(d.ecog_performance_status);

        if (d.estrogen_receptor_status && d.progesterone_receptor_status && d.her2_status) {
          const erNeg   = ['Negative', 'ER-'].includes(d.estrogen_receptor_status);
          const prNeg   = ['Negative', 'PR-'].includes(d.progesterone_receptor_status);
          const her2Neg = ['Negative', 'HER2-'].includes(d.her2_status);
          d.tnbc_status = erNeg && prNeg && her2Neg;
        }

        setPatientInfo(d);
        setEditedInfo(d);

        const user = res.data.user;
        const name = user
          ? (`${user.first_name} ${user.last_name}`.trim() || user.username || `Patient ${personId}`)
          : `Patient ${personId}`;
        setPatientName(name);
        setEditedName(name);
        setFetchError(null);
      } catch (err: any) {
        setFetchError(err.response?.data?.error || 'Failed to fetch patient information');
      } finally {
        setLoading(false);
      }
    })();
  }, [personId]);

  // ── Auto-save (2 s debounce, sequence-safe) ───────────────────────────────
  const doSave = useCallback(async () => {
    const data = pendingDataRef.current;
    if (!data || !personId) return;
    const seq = ++saveSeqRef.current;
    setSaveStatus('saving');
    try {
      await api.patch(`/patient-info/${personId}/`, data.info);
      if (seq === saveSeqRef.current) {
        setSaveStatus('saved');
        setTimeout(() => setSaveStatus(s => (s === 'saved' ? 'idle' : s)), 1200);
      }
    } catch {
      if (seq === saveSeqRef.current) setSaveStatus('error');
    }
  }, [personId]);

  const scheduleAutoSave = useCallback((info: any, name: string) => {
    pendingDataRef.current = { info, name };
    if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current);
    setSaveStatus('pending');
    autoSaveTimerRef.current = setTimeout(() => {
      autoSaveTimerRef.current = null;
      doSave();
    }, 2000);
  }, [doSave]);

  // ── Handlers ──────────────────────────────────────────────────────────────
  const handleFieldChange = useCallback((field: string, value: any) => {
    const base    = pendingDataRef.current?.info ?? editedInfoRef.current;
    const updated = { ...base, [field]: value };

    if (['estrogen_receptor_status', 'progesterone_receptor_status', 'her2_status'].includes(field)) {
      const er   = field === 'estrogen_receptor_status'    ? value : updated.estrogen_receptor_status;
      const pr   = field === 'progesterone_receptor_status' ? value : updated.progesterone_receptor_status;
      const her2 = field === 'her2_status'                 ? value : updated.her2_status;
      const neg  = (v: string) => ['Negative', 'ER-', 'PR-', 'HER2-'].includes(v);
      if (neg(er) && neg(pr) && neg(her2))  updated.tnbc_status = true;
      else if (er || pr || her2)            updated.tnbc_status = false;
    }

    setEditedInfo(updated);
    scheduleAutoSave(updated, editedNameRef.current);
  }, [scheduleAutoSave]);

  const handleNameChange = useCallback((name: string) => {
    setEditedName(name);
    scheduleAutoSave(pendingDataRef.current?.info ?? editedInfoRef.current, name);
  }, [scheduleAutoSave]);

  const handleMutationAdd = useCallback(() => {
    const m = [...(pendingDataRef.current?.info?.genetic_mutations ?? editedInfoRef.current?.genetic_mutations ?? [])];
    m.push({ gene: '', mutation: '', origin: '', interpretation: '' });
    handleFieldChange('genetic_mutations', m);
  }, [handleFieldChange]);

  const handleMutationRemove = useCallback((i: number) => {
    const m = [...(pendingDataRef.current?.info?.genetic_mutations ?? editedInfoRef.current?.genetic_mutations ?? [])];
    m.splice(i, 1);
    handleFieldChange('genetic_mutations', m);
  }, [handleFieldChange]);

  const handleMutationChange = useCallback((i: number, field: string, value: string) => {
    const m = [...(pendingDataRef.current?.info?.genetic_mutations ?? editedInfoRef.current?.genetic_mutations ?? [])];
    m[i] = { ...m[i], [field]: value };
    if (field === 'gene') m[i].mutation = '';
    handleFieldChange('genetic_mutations', m);
  }, [handleFieldChange]);

  const handleZipcodeChange = useCallback(async (zipcode: string) => {
    handleFieldChange('postal_code', zipcode);
    if (zipcode.length === 5 && /^\d{5}$/.test(zipcode)) {
      try {
        const res = await fetch(`https://api.zippopotam.us/us/${zipcode}`);
        if (res.ok) {
          const data = await res.json();
          if (data.places?.length > 0) {
            const place = data.places[0];
            setEditedInfo((prev: any) => {
              const updated = { ...prev, city: place['place name'], region: place['state'] };
              pendingDataRef.current = { info: updated, name: editedNameRef.current };
              return updated;
            });
          }
        }
      } catch {}
    }
  }, [handleFieldChange]);

  const getDiseaseType = (): 'breast' | 'lymphoma' | 'myeloma' | 'cll' | 'other' => {
    const d = editedInfo?.disease?.toLowerCase() || '';
    if (d.includes('breast'))   return 'breast';
    if (d.includes('lymphoma')) return 'lymphoma';
    if (d.includes('myeloma'))  return 'myeloma';
    if (d.includes('cll') || d.includes('chronic lymphocytic')) return 'cll';
    return 'other';
  };

  const getDiseaseTabLabel = () =>
    ({ breast: 'Breast Cancer', lymphoma: 'Follicular Lymphoma', myeloma: 'Multiple Myeloma', cll: 'CLL', other: 'Disease Specific' })[getDiseaseType()];

  // ── Render states ─────────────────────────────────────────────────────────
  if (loading) return <PatientDetailSkeleton />;

  if (fetchError && !patientInfo) {
    return (
      <div className="min-h-screen bg-[#f5f7fa] flex items-center justify-center p-6">
        <div className="max-w-sm w-full rounded-2xl bg-white shadow p-8 text-center">
          <AlertCircle className="h-10 w-10 text-red-400 mx-auto mb-3" />
          <p className="text-sm text-red-700 mb-6">{fetchError}</p>
          <button onClick={() => navigate('/')}
            className="inline-flex items-center gap-2 text-sm font-medium text-portal-brand hover:underline">
            <ArrowLeft className="h-4 w-4" /> Patient List
          </button>
        </div>
      </div>
    );
  }

  const tabLabels = ['General', getDiseaseTabLabel(), 'Treatment', 'Blood', 'Labs', 'Behavior'];
  const tabDescriptions: Record<number, string> = {
    0: 'Keep patient details up to date for accurate personalisation.',
    1: 'Disease-specific clinical information and genetic details.',
    2: 'Therapy history, treatment lines, and planned therapies.',
    3: 'Blood counts, electrolytes, coagulation, and cardiac markers.',
    4: 'Chemistry panel, liver function tests, and other lab markers.',
    5: 'Lifestyle, socioeconomic, and behavioural health factors.',
  };

  const initials  = getInitials(patientName);
  const avatarBg  = getAvatarBg(patientName);
  const branding  = getActiveBranding();

  return (
    <div className="min-h-screen bg-[#f5f7fa]">

      {/* ── Sticky top bar ─────────────────────────────────────────────────── */}
      <div className="sticky top-0 z-20 bg-white/90 backdrop-blur-md border-b border-portal-border shadow-[0_1px_3px_rgba(0,0,0,0.05)]">
        <div className="max-w-5xl mx-auto px-6 h-14 flex items-center justify-between gap-4">

          {/* Left: brand identity + back button */}
          <div className="flex items-center gap-3 flex-shrink-0">
            {branding.logoUrl && (
              <><img src={branding.logoUrl} alt={branding.appName} className="h-6 w-auto" /><div className="h-4 w-px bg-portal-border" /></>
            )}
            {!branding.logoUrl && branding.appName && (
              <><span className="text-sm font-bold text-portal-brand tracking-tight">{branding.appName}</span><div className="h-4 w-px bg-portal-border" /></>
            )}
            <button
              onClick={() => navigate('/')}
              className="h-8 w-8 rounded-full bg-[#f5f7fa] border border-portal-border flex items-center justify-center text-portal-text-secondary hover:text-portal-text-primary hover:bg-[#eef0f4] transition-colors"
              aria-label="Back to patient list"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
            </button>
          </div>

          {/* Right: avatar + name + badges + save status */}
          <div className="flex items-center gap-3 min-w-0">
            <div
              className="flex-shrink-0 h-8 w-8 rounded-full flex items-center justify-center text-white text-[11px] font-bold shadow-sm ring-2 ring-white"
              style={{ backgroundColor: avatarBg }}
            >
              {initials}
            </div>
            <div className="flex items-center gap-2 min-w-0 flex-wrap">
              <span className="text-sm font-semibold text-portal-text-primary truncate">{patientName}</span>
              <span className="inline-flex items-center rounded-full bg-indigo-50 text-indigo-600 px-2 py-0.5 text-[10px] font-bold tracking-wide uppercase flex-shrink-0">
                #{personId}
              </span>
              {editedInfo?.disease && (
                <span className="hidden sm:inline-flex items-center rounded-full bg-emerald-50 text-emerald-700 border border-emerald-100 px-2 py-0.5 text-[10px] font-medium flex-shrink-0">
                  {editedInfo.disease}
                </span>
              )}
            </div>
            <div className="h-5 w-px bg-portal-border flex-shrink-0" />
            <div className="flex-shrink-0">
              <SaveStatusIndicator status={saveStatus} onRetry={doSave} />
            </div>
          </div>
        </div>
      </div>

      {/* ── Page content ───────────────────────────────────────────────────── */}
      <div className="max-w-5xl mx-auto px-6 py-6">

        {/* Tab bar */}
        <div className="border-b border-portal-border mb-5">
          <nav className="flex gap-x-1 overflow-x-auto" aria-label="Patient info tabs">
            {tabLabels.map((label, i) => (
              <button
                key={i}
                onClick={() => setActiveTab(i)}
                className={[
                  'px-3 py-2.5 text-sm font-medium whitespace-nowrap border-b-2 -mb-px transition-colors focus:outline-none',
                  activeTab === i
                    ? 'border-portal-brand text-portal-brand'
                    : 'border-transparent text-portal-text-secondary hover:text-portal-text-primary',
                ].join(' ')}
              >
                {label}
              </button>
            ))}
          </nav>
        </div>

        {/* Content card */}
        <div className="rounded-2xl bg-white shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">

          {/* Card header */}
          <div className="px-8 pt-8 pb-6">
            <h2 className="text-xl font-bold text-portal-text-primary">{tabLabels[activeTab]}</h2>
            <p className="mt-1 text-sm text-portal-text-secondary">{tabDescriptions[activeTab]}</p>
          </div>

          {/* Tab content — key forces re-mount → triggers animation */}
          <div key={activeTab} className="px-8 pb-10 animate-tab-in">
            {activeTab === 0 && (
              <GeneralTab
                formData={editedInfo}
                onChange={handleFieldChange}
                editedName={editedName}
                onNameChange={handleNameChange}
                onZipcodeChange={handleZipcodeChange}
              />
            )}
            {activeTab === 1 && (
              <DiseaseTab
                formData={editedInfo}
                onChange={handleFieldChange}
                onMutationAdd={handleMutationAdd}
                onMutationRemove={handleMutationRemove}
                onMutationChange={handleMutationChange}
                diseaseType={getDiseaseType()}
              />
            )}
            {activeTab === 2 && (
              <TreatmentTab
                formData={editedInfo}
                onChange={handleFieldChange}
                diseaseType={getDiseaseType()}
              />
            )}
            {activeTab === 3 && <BloodTab    formData={editedInfo} onChange={handleFieldChange} />}
            {activeTab === 4 && <LabsTab     formData={editedInfo} onChange={handleFieldChange} />}
            {activeTab === 5 && <BehaviorTab formData={editedInfo} onChange={handleFieldChange} />}
          </div>
        </div>
      </div>
    </div>
  );
};

export default PatientDetail;
