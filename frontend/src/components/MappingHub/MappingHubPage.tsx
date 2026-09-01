import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, Braces, Database, FlaskConical } from 'lucide-react';
import { fetchMappingStats, type MappingStats } from '@/api/mappingHub';

function DiagramBox({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`rounded-md border bg-background px-3 py-2 text-center text-sm font-medium shadow-sm ${className}`}>{children}</div>;
}

function FieldMappingDiagram() {
  const tables = ['Drug', 'Procedure', 'Measurement', 'Observation', 'Condition'];
  return <div className="relative mx-auto mt-8 min-h-64 max-w-5xl pt-2" aria-label="OMOP fields map from five tables into PatientRecord">
    <DiagramBox className="relative z-10 mx-auto w-44 border-blue-300 bg-blue-50 text-blue-950">PatientRecord</DiagramBox>
    <svg className="pointer-events-none absolute inset-x-0 top-10 h-44 w-full" viewBox="0 0 1000 176" preserveAspectRatio="none" aria-hidden="true"><defs><marker id="field-arrow" markerWidth="8" markerHeight="8" refX="4" refY="4" orient="auto"><path d="M 0 8 L 4 0 L 8 8 z" className="fill-blue-400" /></marker></defs>{[100, 300, 500, 700, 900].map(x => <line key={x} x1={x} y1="172" x2="500" y2="5" className="stroke-blue-300" strokeWidth="2" markerEnd="url(#field-arrow)" />)}</svg>
    <div className="absolute inset-x-0 bottom-0 grid grid-cols-5 gap-2 sm:gap-4">{tables.map(table => <DiagramBox key={table} className="border-slate-300 text-xs sm:text-sm">{table}</DiagramBox>)}</div>
  </div>;
}

function CodeMappingDiagram() {
  const systems = ['ICD-10-CM', 'SNOMED CT', 'LOINC', 'RxNorm', 'CPT', 'HCPCS'];
  return <div className="relative mx-auto mt-8 min-h-64 max-w-5xl pt-2" aria-label="Source coding systems map into the OMOP Concept table">
    <DiagramBox className="relative z-10 mx-auto w-48 border-purple-300 bg-purple-50 text-purple-950">OMOP Concept</DiagramBox>
    <svg className="pointer-events-none absolute inset-x-0 top-10 h-44 w-full" viewBox="0 0 1000 176" preserveAspectRatio="none" aria-hidden="true"><defs><marker id="code-arrow" markerWidth="8" markerHeight="8" refX="4" refY="4" orient="auto"><path d="M 0 8 L 4 0 L 8 8 z" className="fill-purple-400" /></marker></defs>{[83, 250, 417, 583, 750, 917].map(x => <line key={x} x1={x} y1="172" x2="500" y2="5" className="stroke-purple-300" strokeWidth="2" markerEnd="url(#code-arrow)" />)}</svg>
    <div className="absolute inset-x-0 bottom-0 grid grid-cols-3 gap-2 sm:grid-cols-6 sm:gap-3">{systems.map(system => <div key={system} className="rounded-md bg-purple-50 px-1 py-2 text-center text-xs font-medium text-purple-950 sm:text-sm">{'{'}{system}{'}'}</div>)}</div>
  </div>;
}

function TherapyMappingDiagram() {
  return <div className="relative mx-auto mt-8 min-h-60 max-w-4xl" aria-label="Therapy components map to regimens and classes, and regimens map to diseases">
    <svg className="pointer-events-none absolute inset-0 h-full w-full" viewBox="0 0 800 240" preserveAspectRatio="none" aria-hidden="true"><defs><marker id="therapy-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M 0 0 L 8 4 L 0 8 z" className="fill-emerald-400" /></marker></defs><line x1="400" y1="190" x2="250" y2="65" className="stroke-emerald-300" strokeWidth="3" markerEnd="url(#therapy-arrow)" /><line x1="400" y1="190" x2="550" y2="65" className="stroke-emerald-300" strokeWidth="3" markerEnd="url(#therapy-arrow)" /><line x1="250" y1="65" x2="250" y2="15" className="stroke-emerald-300" strokeWidth="3" markerEnd="url(#therapy-arrow)" /></svg>
    <DiagramBox className="absolute left-[20%] top-0 z-10 w-32 border-emerald-300 bg-emerald-50 text-emerald-950">Disease</DiagramBox>
    <DiagramBox className="absolute left-[20%] top-20 z-10 w-32 border-emerald-300 bg-emerald-50 text-emerald-950">Regimen</DiagramBox>
    <DiagramBox className="absolute right-[20%] top-20 z-10 w-32 border-emerald-300 bg-emerald-50 text-emerald-950">Class</DiagramBox>
    <DiagramBox className="absolute left-1/2 top-44 z-10 w-32 -translate-x-1/2 border-emerald-300 bg-emerald-50 text-emerald-950">Component</DiagramBox>
  </div>;
}

export default function MappingHubPage() {
  const navigate = useNavigate();
  const [stats, setStats] = useState<MappingStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => { setLoading(true); setError(null); try { setStats(await fetchMappingStats()); } catch { setError('Failed to load mapping statistics.'); } finally { setLoading(false); } }, []);
  useEffect(() => { queueMicrotask(() => { void load(); }); }, [load]);
  const cardClass = 'w-full rounded-xl border border-border bg-background p-6 text-left shadow-sm transition-all hover:border-primary/50 hover:shadow-md sm:p-8';
  const stat = (content: ReactNode) => loading ? <span className="text-sm text-muted-foreground">Loading...</span> : content;

  return <div className="mx-auto max-w-7xl p-6">
    <div className="mb-6 flex items-center gap-3"><button onClick={() => navigate('/')} className="rounded p-1 text-muted-foreground hover:bg-accent" aria-label="Back to patients"><ArrowLeft size={20} /></button><div><h1 className="text-2xl font-bold text-foreground">Mapping Administration</h1><p className="text-sm text-muted-foreground">See how clinical fields, source codes, and therapies connect to OMOP.</p></div></div>
    {error && <div className="mb-4 rounded-md bg-destructive/10 p-4 text-sm text-destructive">{error}</div>}
    <div className="space-y-6">
      <button onClick={() => navigate('/field-mappings')} className={cardClass}><div className="flex flex-wrap items-start justify-between gap-4"><div className="flex items-center gap-3"><div className="rounded-lg bg-blue-50 p-2.5 text-blue-600"><Database size={22} /></div><div><h2 className="text-lg font-semibold text-foreground">Field Mapping</h2><p className="text-sm text-muted-foreground">Map PatientRecord fields to their OMOP destinations.</p></div></div>{stat(stats && <div className="flex gap-3 text-sm"><span>{stats.field_mappings.total} fields</span><span className="text-green-600">{stats.field_mappings.approved} approved</span><span className="text-amber-600">{stats.field_mappings.proposed} proposed</span><span className="text-gray-400">{stats.field_mappings.unmapped} unmapped</span></div>)}</div><FieldMappingDiagram /></button>
      <button onClick={() => navigate('/code-mappings')} className={cardClass}><div className="flex flex-wrap items-start justify-between gap-4"><div className="flex items-center gap-3"><div className="rounded-lg bg-purple-50 p-2.5 text-purple-600"><Braces size={22} /></div><div><h2 className="text-lg font-semibold text-foreground">Code Mapping</h2><p className="text-sm text-muted-foreground">Normalize source coding systems into the OMOP vocabulary.</p></div></div>{stat(stats && <div className="flex gap-3 text-sm"><span>{stats.code_mappings.total} mappings</span><span className="text-green-600">{stats.code_mappings.approved} approved</span><span className="text-amber-600">{stats.code_mappings.proposed} proposed</span></div>)}</div><CodeMappingDiagram /></button>
      <button onClick={() => navigate('/therapy-mappings')} className={cardClass}><div className="flex flex-wrap items-start justify-between gap-4"><div className="flex items-center gap-3"><div className="rounded-lg bg-emerald-50 p-2.5 text-emerald-600"><FlaskConical size={22} /></div><div><h2 className="text-lg font-semibold text-foreground">Therapy Mapping</h2><p className="text-sm text-muted-foreground">Build therapy regimens from components and relate them to diseases.</p></div></div>{stat(stats && <div className="flex gap-3 text-sm"><span>{stats.therapy.regimens} regimens</span><span>{stats.therapy.components} components</span><span>{stats.therapy.classes} classes</span><span>{stats.therapy.disease_links} disease links</span></div>)}</div><TherapyMappingDiagram /></button>
    </div>
  </div>;
}
