import PageTitle from "@/components/Branding/PageTitle";
import api from "@/api/axios";
import { useEffect, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { editMappingUrl } from "./mappingLinks";

const UNATTRIBUTED = "__unattributed__";

interface FacilityEntry { facility: string; system_count: number; code_count: number }
interface SystemEntry { source_vocabulary_id: string; code_count: number; shared_with_facilities: number }
interface CodeRow {
  id: number; source_vocabulary_id: string; source_code: string; description: string;
  omop_table: string; status: string; destination_concept_id: number | null;
  destination_concept_name: string; destination_vocabulary_id: string;
  destination_concept_code: string; destination_unit: string; seen: number;
  axis_variant_count: number;
}
interface CodesResponse {
  facility: string; systems: SystemEntry[]; results: CodeRow[];
  total: number; page: number; page_size: number;
}

const TABLES = ["measurement", "observation", "condition_occurrence", "drug_exposure", "procedure_occurrence"];

function label(facility: string) {
  return facility === UNATTRIBUTED ? "Unattributed" : facility;
}

function FacilityList({ search }: { search: string }) {
  const [, setParams] = useSearchParams();
  const [state, setState] = useState<{ key: string; list: FacilityEntry[] | null; error: boolean } | null>(null);
  useEffect(() => {
    let active = true;
    void api.get<{ facilities: FacilityEntry[] }>("/v1/code-mappings/facility-browse/", { params: { search } })
      .then(({ data }) => { if (active) setState({ key: search, list: data.facilities, error: false }); })
      .catch(() => { if (active) setState({ key: search, list: null, error: true }); });
    return () => { active = false; };
  }, [search]);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const value = String(new FormData(event.currentTarget).get("search") || "").trim();
    setParams(value ? { search: value } : {});
  };
  const loading = state?.key !== search;
  return <>
    <form onSubmit={submit} className="mt-5 flex gap-2">
      <label className="text-sm font-medium text-slate-700">Hospital / lab / facility
        <input name="search" defaultValue={search} placeholder="Search facility names"
          className="mt-1 block w-72 rounded border border-slate-300 px-3 py-2 text-sm" />
      </label>
      <button type="submit" className="self-end rounded bg-sky-800 px-4 py-2 text-sm text-white">Search</button>
    </form>
    {loading && <p role="status" className="mt-4 text-sm text-slate-600">Loading facilities…</p>}
    {!loading && state?.error && <p role="alert" className="mt-4 text-sm text-red-700">Could not load facilities.</p>}
    {!loading && state?.list && (state.list.length === 0
      ? <p className="mt-4 text-sm text-slate-600">No facilities match.</p>
      : <table aria-label="Facilities" className="mt-4 w-full max-w-3xl text-left text-sm">
        <thead className="border-b border-slate-200 text-xs uppercase text-slate-600"><tr>
          <th className="px-2 py-2">Facility</th><th className="px-2 py-2 text-right">Source systems</th>
          <th className="px-2 py-2 text-right">Source codes</th></tr></thead>
        <tbody className="divide-y divide-slate-100">{state.list.map((f) => <tr key={f.facility}>
          <td className="px-2 py-2"><Link className="text-sky-800 underline"
            to={`?${new URLSearchParams({ facility: f.facility })}`}>{label(f.facility)}</Link></td>
          <td className="px-2 py-2 text-right tabular-nums">{f.system_count.toLocaleString()}</td>
          <td className="px-2 py-2 text-right tabular-nums">{f.code_count.toLocaleString()}</td>
        </tr>)}</tbody></table>)}
  </>;
}

function FacilityCodes({ facility }: { facility: string }) {
  const [params, setParams] = useSearchParams();
  const system = params.get("system") || "";
  const q = params.get("q") || "";
  const status = params.get("status") || "";
  const table = params.get("omop_table") || "";
  const sort = params.get("sort") || "code";
  const page = Number(params.get("page") || "1");
  const key = JSON.stringify([facility, system, q, status, table, sort, page]);
  const [state, setState] = useState<{ key: string; data: CodesResponse | null; error: boolean } | null>(null);
  useEffect(() => {
    let active = true;
    void api.get<CodesResponse>("/v1/code-mappings/facility-browse/", {
      params: { facility, system, q, status, omop_table: table, sort, page },
    }).then(({ data }) => { if (active) setState({ key, data, error: false }); })
      .catch(() => { if (active) setState({ key, data: null, error: true }); });
    return () => { active = false; };
  }, [key, facility, system, q, status, table, sort, page]);
  const loading = state?.key !== key;
  const data = loading ? null : state?.data;

  const update = (changes: Record<string, string>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) { if (v) next.set(k, v); else next.delete(k); }
    if (!("page" in changes)) next.delete("page");
    setParams(next);
  };
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    update({ q: String(new FormData(event.currentTarget).get("q") || "").trim() });
  };
  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;
  const select = "mt-1 block rounded border border-slate-300 px-2 py-2 text-sm";

  return <>
    <p className="mt-2 text-sm"><Link to="?" className="text-sky-700 underline">← All facilities</Link></p>
    <h2 className="mt-2 text-lg font-semibold">{label(facility)}</h2>
    {facility === UNATTRIBUTED && <p className="text-sm text-slate-600">
      Source systems with no observed or confirmed facility association.</p>}
    <form key={q} onSubmit={submit} className="mt-3 flex flex-wrap items-end gap-3 rounded-md border border-slate-200 bg-white p-3">
      <label className="text-sm font-medium text-slate-700">Source code or description
        <input name="q" defaultValue={q} placeholder="e.g. M spike, 7761"
          className="mt-1 block w-64 rounded border border-slate-300 px-3 py-2 text-sm" /></label>
      <label className="text-sm font-medium text-slate-700">Source code system
        <select aria-label="Source code system" value={system} onChange={(e) => update({ system: e.target.value })} className={select}>
          <option value="">All systems</option>
          {data?.systems.map((s) => <option key={s.source_vocabulary_id} value={s.source_vocabulary_id}>
            {s.source_vocabulary_id} ({s.code_count.toLocaleString()})</option>)}
        </select></label>
      <label className="text-sm font-medium text-slate-700">Status
        <select aria-label="Status" value={status} onChange={(e) => update({ status: e.target.value })} className={select}>
          <option value="">Any</option>
          {["unmapped", "proposed", "approved", "rejected"].map((s) => <option key={s} value={s}>{s}</option>)}
        </select></label>
      <label className="text-sm font-medium text-slate-700">Domain
        <select aria-label="Domain" value={table} onChange={(e) => update({ omop_table: e.target.value })} className={select}>
          <option value="">Any</option>
          {TABLES.map((t) => <option key={t} value={t}>{t}</option>)}
        </select></label>
      <label className="text-sm font-medium text-slate-700">Sort
        <select aria-label="Sort" value={sort} onChange={(e) => update({ sort: e.target.value === "code" ? "" : e.target.value })} className={select}>
          <option value="code">Source code number</option>
          <option value="-code">Source code number, descending</option>
          <option value="seen">Seen (most first)</option>
          <option value="description">Description</option>
        </select></label>
      <button type="submit" className="rounded bg-sky-800 px-4 py-2 text-sm text-white">Search</button>
    </form>
    {data?.systems.some((s) => s.shared_with_facilities > 0) && <p className="mt-2 text-xs text-slate-600">
      A system marked “shared” is also used by other facilities (for example an outside laboratory); identical codes in different systems are not necessarily the same test.</p>}
    {loading && <p role="status" className="mt-4 text-sm text-slate-600">Loading source codes…</p>}
    {!loading && state?.error && <p role="alert" className="mt-4 text-sm text-red-700">Could not load source codes.</p>}
    {data && (data.results.length === 0
      ? <p className="mt-4 text-sm text-slate-600">No source codes match.</p>
      : <div className="mt-4 overflow-x-auto rounded-md border border-slate-200 bg-white">
        <table aria-label="Source codes" className="w-full min-w-[900px] text-left text-sm">
          <thead className="bg-slate-100 text-xs uppercase text-slate-600"><tr>
            <th className="px-3 py-2">System</th><th className="px-3 py-2">Code</th>
            <th className="px-3 py-2">Description</th><th className="px-3 py-2">Domain</th>
            <th className="px-3 py-2">Status</th><th className="px-3 py-2">Destination</th>
            <th className="px-3 py-2">Unit</th><th className="px-3 py-2 text-right">Seen</th>
            <th className="px-3 py-2">Axis values</th><th className="px-3 py-2" /></tr></thead>
          <tbody className="divide-y divide-slate-100">{data.results.map((r) => {
            const shared = data.systems.find((s) => s.source_vocabulary_id === r.source_vocabulary_id)?.shared_with_facilities;
            return <tr key={r.id}>
              <td className="max-w-[200px] break-all px-3 py-2 font-mono text-xs">{r.source_vocabulary_id}
                {!!shared && <span className="ml-1 rounded bg-amber-100 px-1 text-[10px]">shared</span>}</td>
              <td className="px-3 py-2 font-mono text-xs">{r.source_code}</td>
              <td className="px-3 py-2">{r.description || "—"}</td>
              <td className="px-3 py-2 text-xs">{r.omop_table || "—"}</td>
              <td className="px-3 py-2 capitalize">{r.destination_concept_id ? r.status : r.status === "rejected" ? "rejected" : "unmapped"}</td>
              <td className="px-3 py-2">{r.destination_concept_id
                ? <>{r.destination_concept_name}<span className="block text-xs text-slate-500">{r.destination_vocabulary_id} {r.destination_concept_code}</span></>
                : "—"}</td>
              <td className="px-3 py-2 text-xs">{r.destination_unit || "—"}</td>
              <td className="px-3 py-2 text-right tabular-nums">{r.seen.toLocaleString()}</td>
              <td className="px-3 py-2 text-xs">{r.axis_variant_count > 0 ? `${r.axis_variant_count} axis values` : "—"}</td>
              <td className="px-3 py-2"><a href={editMappingUrl(r.id)} target="_blank" rel="noreferrer"
                className="text-sky-800 underline">Edit mapping</a></td>
            </tr>;
          })}</tbody>
        </table>
      </div>)}
    {data && data.total > data.page_size && <div className="mt-4 flex items-center justify-between text-sm">
      <button type="button" disabled={page <= 1} onClick={() => update({ page: String(page - 1) })} className="text-sky-700 underline disabled:text-slate-400">Previous</button>
      <span>Page {data.page} of {totalPages} · {data.total.toLocaleString()} codes</span>
      <button type="button" disabled={page >= totalPages} onClick={() => update({ page: String(page + 1) })} className="text-sky-700 underline disabled:text-slate-400">Next</button>
    </div>}
  </>;
}

export default function FacilityBrowsePage() {
  const [params] = useSearchParams();
  const facility = params.get("facility") || "";
  return <div className="min-h-screen bg-slate-50 p-6"><main className="mx-auto max-w-7xl">
    <Link to="/code-mappings" className="text-sm text-sky-700 underline">← Code Mapping</Link>
    <PageTitle className="mt-4 text-2xl font-semibold text-slate-950">Browse Source Codes by Facility</PageTitle>
    <p className="mt-2 max-w-4xl text-sm text-slate-600">
      Pick a hospital or laboratory, then browse its source code systems and codes, including sibling codes no sampled patient carries. Facilities come from observed or curator-confirmed associations; Edit mapping opens the existing mapping form.
    </p>
    {facility ? <FacilityCodes facility={facility} /> : <FacilityList search={params.get("search") || ""} />}
  </main></div>;
}
