import PageTitle from "@/components/Branding/PageTitle";
import api from "@/api/axios";
import { useEffect, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";

interface Association {
  source_vocabulary_id: string;
  facility_name: string;
  facility_id: string;
  level: string;
  confidence: string;
  attribution_method: string;
  provenance: string;
}

interface RegistryResponse {
  results: Association[];
  total: number;
  page: number;
  page_size: number;
  seed_import: {
    outcome: string;
    expected_rows: number;
    completed_at: string | null;
    failure_reason: string;
  } | null;
  system_diagnostics: {
    source_code_rows: number;
    codes_with_facility_evidence: number;
  } | null;
}

export default function SourceSystemManagementPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const system = searchParams.get("system") || "";
  const facility = searchParams.get("facility") || "";
  const page = Number(searchParams.get("page") || "1");
  const requestKey = JSON.stringify([system, facility, page]);
  const [result, setResult] = useState<{ key: string; data: RegistryResponse | null; error: string } | null>(null);
  const loading = result?.key !== requestKey;
  const data = loading ? null : result?.data;
  const error = loading ? "" : result?.error || "";

  useEffect(() => {
    let active = true;
    void api.get<RegistryResponse>("/v1/code-mappings/source-systems/", {
      params: { system, facility, page },
    }).then(({ data: response }) => {
      if (active) setResult({ key: requestKey, data: response, error: "" });
    }).catch(() => {
      if (active) setResult({ key: requestKey, data: null, error: "Could not load source-code system associations." });
    });
    return () => { active = false; };
  }, [system, facility, page, requestKey]);

  const applyFilters = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const systemInput = String(values.get("system") || "").trim();
    const facilityInput = String(values.get("facility") || "").trim();
    const next = new URLSearchParams();
    if (systemInput) next.set("system", systemInput);
    if (facilityInput) next.set("facility", facilityInput);
    setSearchParams(next);
  };
  const movePage = (nextPage: number) => {
    const next = new URLSearchParams(searchParams);
    next.set("page", String(nextPage));
    setSearchParams(next);
  };

  const receipt = data?.seed_import;
  const diagnostics = data?.system_diagnostics;
  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return <div className="min-h-screen bg-slate-50 p-6"><main className="mx-auto max-w-7xl">
    <Link to="/code-mappings" className="text-sm text-sky-700 underline">← Code Mapping</Link>
    <PageTitle className="mt-4 text-2xl font-semibold text-slate-950">Source Code Systems/Hospitals Management</PageTitle>
    <p className="mt-2 max-w-4xl text-sm text-slate-600">
      Read-only observations from the governed hospital-code seed. One exact FHIR Coding.system can have several hospitals, sites, brands, or aliases. These names do not establish a verified Organization or change how a source code maps to OMOP.
    </p>

    <form key={`${system}|${facility}`} onSubmit={applyFilters} className="mt-5 grid gap-3 rounded-md border border-slate-200 bg-white p-4 md:grid-cols-[2fr_1fr_auto] md:items-end">
      <label className="text-sm font-medium text-slate-700">Exact FHIR Coding.system
        <input name="system" defaultValue={system}
          placeholder="urn:oid:…" className="mt-1 w-full rounded border border-slate-300 px-3 py-2 font-mono text-xs" />
      </label>
      <label className="text-sm font-medium text-slate-700">Hospital / facility name contains
        <input name="facility" defaultValue={facility}
          placeholder="Search observed names" className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm" />
      </label>
      <button type="submit" className="rounded bg-sky-800 px-4 py-2 text-sm font-medium text-white hover:bg-sky-900">Search</button>
    </form>

    {data && <div className="mt-4 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
      <div className="rounded border border-slate-200 bg-white p-3">
        <span className="block text-xs font-semibold uppercase text-slate-500">Matching associations</span>
        <span className="font-medium">{data.total.toLocaleString()}</span>
      </div>
      <div className="rounded border border-slate-200 bg-white p-3">
        <span className="block text-xs font-semibold uppercase text-slate-500">Pinned seed import</span>
        <span className="font-medium">{receipt ? receipt.outcome : "No receipt recorded"}</span>
        {receipt?.completed_at && <span className="block text-xs text-slate-500">Completed {new Date(receipt.completed_at).toLocaleString()}</span>}
        {receipt?.failure_reason && <span className="block text-xs text-red-700">{receipt.failure_reason}</span>}
      </div>
      {diagnostics && <div className="rounded border border-slate-200 bg-white p-3">
        <span className="block text-xs font-semibold uppercase text-slate-500">Codes under this exact system</span>
        <span className="font-medium">{diagnostics.source_code_rows.toLocaleString()}</span>
        <span className="block text-xs text-slate-500">{diagnostics.codes_with_facility_evidence.toLocaleString()} with per-code facility evidence</span>
      </div>}
    </div>}

    {loading && <p role="status" className="mt-5 text-sm text-slate-600">Loading source-code systems…</p>}
    {error && <p role="alert" className="mt-5 text-sm text-red-700">{error}</p>}
    {!loading && data && <>
      {data.results.length === 0 ? <div className="mt-5 rounded border border-amber-200 bg-amber-50 p-4 text-sm text-slate-800">
        <p className="font-medium">No observed facility associations match these filters.</p>
        {system && diagnostics && diagnostics.codes_with_facility_evidence > 0 && !facility && <p className="mt-1">This system has per-code facility evidence but no registry rows. Check the pinned seed import status above; its asynchronous replay may not have completed.</p>}
        {system && diagnostics && diagnostics.codes_with_facility_evidence === 0 && <p className="mt-1">No source code under this exact system carries facility evidence in the current mapping table.</p>}
        {facility && <p className="mt-1">Clear the facility filter to see every name recorded for this system.</p>}
      </div> : <div className="mt-5 overflow-x-auto rounded-md border border-slate-200 bg-white">
        <table aria-label="Source code systems and observed hospitals" className="w-full min-w-[900px] text-left text-sm">
          <thead className="bg-slate-100 text-xs uppercase text-slate-600"><tr>
            <th className="px-3 py-3">Exact FHIR Coding.system</th>
            <th className="px-3 py-3">Observed hospital / facility</th>
            <th className="px-3 py-3">Source ID</th>
            <th className="px-3 py-3">Scope</th>
            <th className="px-3 py-3">Confidence</th>
            <th className="px-3 py-3">Attribution</th>
            <th className="px-3 py-3">Provenance</th>
          </tr></thead>
          <tbody className="divide-y divide-slate-200">{data.results.map((row, index) => <tr key={`${row.source_vocabulary_id}|${row.facility_name}|${row.facility_id}|${index}`}>
            <td className="max-w-[300px] break-all px-3 py-3 font-mono text-xs">{row.source_vocabulary_id}</td>
            <td className="px-3 py-3 font-medium">{row.facility_name}</td>
            <td className="max-w-[160px] break-all px-3 py-3 text-xs">{row.facility_id || "—"}</td>
            <td className="px-3 py-3">{row.level || "unspecified"}</td>
            <td className="px-3 py-3">{row.confidence || "unspecified"}</td>
            <td className="px-3 py-3 text-xs">{row.attribution_method || "—"}</td>
            <td className="px-3 py-3 text-xs">{row.provenance}</td>
          </tr>)}</tbody>
        </table>
      </div>}
      {data.total > data.page_size && <div className="mt-4 flex items-center justify-between text-sm">
        <button type="button" disabled={page <= 1} onClick={() => movePage(page - 1)} className="text-sky-700 underline disabled:text-slate-400">Previous</button>
        <span>Page {page} of {totalPages}</span>
        <button type="button" disabled={page >= totalPages} onClick={() => movePage(page + 1)} className="text-sky-700 underline disabled:text-slate-400">Next</button>
      </div>}
    </>}
  </main></div>;
}
