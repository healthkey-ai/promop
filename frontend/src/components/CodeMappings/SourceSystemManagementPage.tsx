import PageTitle from "@/components/Branding/PageTitle";
import api from "@/api/axios";
import { useEffect, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";

interface Association {
  id: number;
  facility_name: string;
  facility_id: string;
  level: string;
  confidence: string;
  attribution_method: string;
  provenance: string;
  review_status: "unreviewed" | "confirmed" | "rejected";
  reviewed_by: string;
  reviewed_at: string | null;
}

interface SystemGroup {
  source_vocabulary_id: string;
  facilities: Association[];
}

interface RegistryResponse {
  results: SystemGroup[];
  total: number;
  association_total: number;
  page: number;
  page_size: number;
  sort: "system_asc" | "system_desc";
  seed_import: {
    artifact_identity: string;
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

function SystemRow({ group, open, onToggle, onMutate }: {
  group: SystemGroup; open: boolean; onToggle: () => void; onMutate: () => void;
}) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const review = async (id: number, review_status: Association["review_status"]) => {
    setSaving(true);
    setError("");
    try {
      await api.patch("/v1/code-mappings/source-systems/", { id, review_status });
      onMutate();
    } catch {
      setError("Could not save the review decision.");
    } finally {
      setSaving(false);
    }
  };
  const addFacility = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    setSaving(true);
    setError("");
    try {
      await api.post("/v1/code-mappings/source-systems/", {
        source_vocabulary_id: group.source_vocabulary_id,
        facility_name: String(values.get("facility_name") || "").trim(),
        facility_id: String(values.get("facility_id") || "").trim(),
      });
      form.reset();
      onMutate();
    } catch {
      setError("Could not add this facility. Check for an existing association.");
    } finally {
      setSaving(false);
    }
  };
  return <>
    <tr className="border-t border-slate-200 bg-slate-50">
      <td className="px-3 py-3">
        <button type="button" onClick={onToggle} aria-expanded={open}
          aria-label={`${open ? "Collapse" : "Expand"} ${group.source_vocabulary_id}`}
          className="flex w-full items-start gap-2 text-left text-sky-800 hover:underline">
          <span aria-hidden="true">{open ? "▾" : "▸"}</span>
          <code className="break-all text-xs">{group.source_vocabulary_id}</code>
        </button>
      </td>
      <td className="px-3 py-3 text-right tabular-nums">{group.facilities.length.toLocaleString()}</td>
    </tr>
    {open && <tr><td colSpan={2} className="px-5 pb-4 pt-1">
      {error && <p role="alert" className="mb-2 text-sm text-red-700">{error}</p>}
      {group.facilities.length ? <table aria-label={`Facilities for ${group.source_vocabulary_id}`} className="w-full text-left text-sm">
        <thead className="border-b border-slate-200 text-xs uppercase text-slate-600"><tr>
          <th className="px-2 py-2">Observed hospital / facility</th>
          <th className="px-2 py-2">Facility ID</th>
          <th className="px-2 py-2">Scope</th>
          <th className="px-2 py-2">Confidence</th>
          <th className="px-2 py-2">Attribution</th>
          <th className="px-2 py-2">Provenance</th>
          <th className="px-2 py-2">Review</th>
        </tr></thead>
        <tbody className="divide-y divide-slate-100">{group.facilities.map((row) => <tr key={row.id} className={row.review_status === "rejected" ? "bg-slate-100 text-slate-500" : ""}>
          <td className="px-2 py-2 font-medium">{row.facility_name}</td>
          <td className="max-w-[160px] break-all px-2 py-2 text-xs">{row.facility_id || "—"}</td>
          <td className="px-2 py-2">{row.level || "unspecified"}</td>
          <td className="px-2 py-2">{row.confidence || "unspecified"}</td>
          <td className="px-2 py-2 text-xs">{row.attribution_method || "—"}</td>
          <td className="px-2 py-2 text-xs">{row.provenance}</td>
          <td className="px-2 py-2 text-xs">
            <span className="block capitalize" title={row.reviewed_by ? `Reviewed by ${row.reviewed_by}` : undefined}>{row.review_status}</span>
            {row.review_status !== "confirmed" && <button type="button" disabled={saving}
              onClick={() => void review(row.id, "confirmed")} className="mr-2 text-sky-700 underline disabled:opacity-50">Confirm</button>}
            {row.review_status !== "rejected" && <button type="button" disabled={saving}
              onClick={() => void review(row.id, "rejected")} className="text-red-700 underline disabled:opacity-50">Reject</button>}
          </td>
        </tr>)}</tbody>
      </table> : <p className="rounded border border-amber-200 bg-amber-50 p-3 text-sm text-slate-700">
        Source codes exist under this system, but no observed facility association is recorded. Check the seed import status and per-code evidence above.
      </p>}
      <form onSubmit={(event) => void addFacility(event)} className="mt-3 flex flex-wrap items-end gap-2 rounded border border-slate-200 p-3">
        <label className="text-xs font-medium text-slate-700">Facility name
          <input name="facility_name" required maxLength={255} className="mt-1 block rounded border border-slate-300 px-2 py-1 text-sm" />
        </label>
        <label className="text-xs font-medium text-slate-700">Facility ID (optional)
          <input name="facility_id" maxLength={255} className="mt-1 block rounded border border-slate-300 px-2 py-1 text-sm" />
        </label>
        <button type="submit" disabled={saving} className="rounded bg-sky-800 px-3 py-1.5 text-sm text-white disabled:opacity-50">Add facility association</button>
      </form>
    </td></tr>}
  </>;
}

export default function SourceSystemManagementPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [refresh, setRefresh] = useState(0);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const system = searchParams.get("system") || "";
  const facility = searchParams.get("facility") || "";
  const page = Number(searchParams.get("page") || "1");
  const sort = searchParams.get("sort") === "system_desc" ? "system_desc" : "system_asc";
  const requestKey = JSON.stringify([system, facility, page, sort, refresh]);
  const [result, setResult] = useState<{ key: string; data: RegistryResponse | null; error: string } | null>(null);
  const loading = result?.key !== requestKey;
  const data = loading ? null : result?.data;
  const error = loading ? "" : result?.error || "";

  useEffect(() => {
    let active = true;
    void api.get<RegistryResponse>("/v1/code-mappings/source-systems/", {
      params: { system, facility, page, sort },
    }).then(({ data: response }) => {
      if (active) setResult({ key: requestKey, data: response, error: "" });
    }).catch(() => {
      if (active) setResult({ key: requestKey, data: null, error: "Could not load source-code system associations." });
    });
    return () => { active = false; };
  }, [system, facility, page, sort, requestKey]);

  const applyFilters = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const systemInput = String(values.get("system") || "").trim();
    const facilityInput = String(values.get("facility") || "").trim();
    const next = new URLSearchParams();
    if (systemInput) next.set("system", systemInput);
    if (facilityInput) next.set("facility", facilityInput);
    if (sort !== "system_asc") next.set("sort", sort);
    setSearchParams(next);
  };
  const movePage = (nextPage: number) => {
    const next = new URLSearchParams(searchParams);
    next.set("page", String(nextPage));
    setSearchParams(next);
  };
  const toggleSort = () => {
    const next = new URLSearchParams(searchParams);
    next.set("sort", sort === "system_asc" ? "system_desc" : "system_asc");
    next.delete("page");
    setSearchParams(next);
  };

  const receipt = data?.seed_import;
  const diagnostics = data?.system_diagnostics;
  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return <div className="min-h-screen bg-slate-50 p-6"><main className="mx-auto max-w-7xl">
    <Link to="/code-mappings" className="text-sm text-sky-700 underline">← Code Mapping</Link>
    <PageTitle className="mt-4 text-2xl font-semibold text-slate-950">Source Code Systems/Hospitals Management</PageTitle>
    <p className="mt-2 max-w-4xl text-sm text-slate-600">
      Manage observed and curator-confirmed facility associations for each exact FHIR Coding.system. Multiple hospitals may use one system. Confirming an association records a curator decision, not verified Organization ownership, and never changes the source code or its OMOP mapping.
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
        <span className="block text-xs font-semibold uppercase text-slate-500">Matching source systems</span>
        <span className="font-medium">{data.total.toLocaleString()}</span>
        <span className="block text-xs text-slate-500">{data.association_total.toLocaleString()} facility associations (including rejected)</span>
      </div>
      <div className="rounded border border-slate-200 bg-white p-3">
        <span className="block text-xs font-semibold uppercase text-slate-500">Latest hospital seed import</span>
        <span className="font-medium">{receipt ? receipt.outcome : "No receipt recorded"}</span>
        {receipt?.artifact_identity && <span className="block break-all text-xs text-slate-500">{receipt.artifact_identity}</span>}
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
        <p className="font-medium">No source systems match these filters.</p>
        {system && diagnostics && diagnostics.codes_with_facility_evidence > 0 && !facility && <p className="mt-1">This system has per-code facility evidence but no registry rows. Check the pinned seed import status above; its asynchronous replay may not have completed.</p>}
        {system && diagnostics && diagnostics.codes_with_facility_evidence === 0 && <p className="mt-1">No source code under this exact system carries facility evidence in the current mapping table.</p>}
        {facility && <p className="mt-1">Clear the facility filter to see every name recorded for this system.</p>}
      </div> : <div className="mt-5 overflow-x-auto rounded-md border border-slate-200 bg-white">
        <table aria-label="Source code systems and observed hospitals" className="w-full min-w-[900px] text-left text-sm">
          <thead className="bg-slate-100 text-xs uppercase text-slate-600"><tr>
            <th className="px-3 py-3"><button type="button" onClick={toggleSort} aria-label="Sort by source code system"
              className="text-sky-800 hover:underline">Exact FHIR Coding.system {sort === "system_asc" ? "↑" : "↓"}</button></th>
            <th className="px-3 py-3 text-right">Facility associations</th>
          </tr></thead>
          <tbody>{data.results.map((group) => <SystemRow key={group.source_vocabulary_id}
            group={group} open={expanded[group.source_vocabulary_id] ?? system === group.source_vocabulary_id}
            onToggle={() => setExpanded((current) => ({ ...current,
              [group.source_vocabulary_id]: !(current[group.source_vocabulary_id] ?? system === group.source_vocabulary_id),
            }))}
            onMutate={() => setRefresh((value) => value + 1)} />)}</tbody>
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
