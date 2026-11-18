import { useState } from "react";
import { Download } from "lucide-react";
import api from "@/api/axios";
import { blobErrorMessage, filenameFromDisposition, saveBlob } from "@/utils/download";
import type { WearableSourceSystem } from "./codeMappingTypes";
import WearableSourceSystemSelect from "./WearableSourceSystemSelect";

// What each code is mapped to in the file. The OMOP concept is the default and
// is asked for by sending nothing, as before; a CDISC Biomedical Concept is
// reached through the OMOP concept a code maps to.
export type Destination = "omop" | "cdisc-bc";
export type FileFormat = "csv" | "docx";
/** Fetches the file; the response's `content-disposition` names it. `sources`
 *  are the source code systems to limit it to; none is every system. */
export type FetchWearableFile = (format: FileFormat, destination: Destination, sources: string[]) => Promise<{
  data: Blob; headers?: Record<string, unknown>;
}>;

const fromTheTab: FetchWearableFile = (format, destination, sources) => api.get<Blob>("/v1/code-mappings/wearables-export/", {
  params: {
    file_format: format,
    ...(destination === "omop" ? {} : { destination }),
    ...(sources.length ? { sources: sources.join(",") } : {}),
  },
  responseType: "blob",
});

/** The CDISC Biomedical Concepts' own vocabulary on the Wearables tab. */
const CDISC_BC = "CDISC-BC";
const LABEL = "Download all wearable mappings";
const CHOICES: { format: FileFormat; label: string; name: string; title: string }[] = [
  { format: "csv", label: "CSV", name: `${LABEL} as CSV`, title: "One line per mapping, for a spreadsheet" },
  {
    format: "docx", label: "Word document", name: `${LABEL} as a Word document`,
    title: "A table per category, for reading and sharing",
  },
];

/**
 * The whole Wearables tab as a file the server builds: every mapping on the
 * tab, whatever section or page it is on, rejected ones aside. The section
 * Download menus beside it write only the rows the page has loaded.
 *
 * The target menu picks what the codes are mapped to in the file: the OMOP
 * concept, or the CDISC Biomedical Concept linked to it.
 *
 * The source system menu limits the file to one source code system; the
 * default is all of them. It is offered when `systems` are given, and not on
 * the public vote page, where the share already decides which systems it holds.
 *
 * Reading the tab is enough to download it, so a read-only reader sees this
 * too. `fetchFile` replaces the signed-in request, for the public vote page,
 * which asks its own endpoint with the share's token.
 */
export default function WearableMappingsDownload({ fetchFile = fromTheTab, systems = [] }: {
  fetchFile?: FetchWearableFile;
  systems?: WearableSourceSystem[];
}) {
  const [busy, setBusy] = useState<FileFormat | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [destination, setDestination] = useState<Destination>("omop");
  const [source, setSource] = useState("");
  // The Biomedical Concepts are what that target maps to, so under it the CDISC
  // BC system has no codes of its own and the file would be empty.
  const offered = destination === "cdisc-bc" ? systems.filter((system) => system.id !== CDISC_BC) : systems;
  const chosen = offered.some((system) => system.id === source) ? source : "";

  const download = async (format: FileFormat) => {
    setBusy(format);
    setError(null);
    try {
      const response = await fetchFile(format, destination, chosen ? [chosen] : []);
      const stem = destination === "omop" ? "wearable-mappings" : "wearable-cdisc-bc-mappings";
      saveBlob(response.data, filenameFromDisposition(
        response.headers?.["content-disposition"], `${stem}.${format}`));
    } catch (err) {
      setError((await blobErrorMessage(err)) ?? "Could not download the wearable mappings. Try again.");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="ml-auto flex flex-wrap items-center justify-end gap-2 text-xs text-slate-600">
      {error && <span role="alert" className="text-red-700">{error}</span>}
      <div role="group" aria-label={LABEL} className="flex items-center gap-2">
        <span className="inline-flex items-center gap-1 font-medium text-slate-700">
          <Download size={12} aria-hidden="true" /> {LABEL}
        </span>
        <select
          aria-label="Map codes to"
          title="What each wearable code is mapped to in the file"
          value={destination}
          disabled={busy !== null}
          onChange={(e) => setDestination(e.target.value as Destination)}
          className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-800"
        >
          <option value="omop">OMOP concept</option>
          <option value="cdisc-bc">CDISC Biomedical Concept</option>
        </select>
        <WearableSourceSystemSelect systems={offered} value={chosen} onChange={setSource} disabled={busy !== null} />
        {CHOICES.map(({ format, label, name, title }) => (
          <button
            key={format}
            type="button"
            title={title}
            // Its own name, not the bare "CSV" the section menus' choices
            // carry; while it works the button is named by what it shows.
            aria-label={busy === format ? undefined : name}
            disabled={busy !== null}
            aria-busy={busy === format}
            onClick={() => { void download(format); }}
            className="rounded-md border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-800 hover:bg-slate-100 focus:outline-2 focus:outline-slate-700 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {busy === format ? "Preparing…" : label}
          </button>
        ))}
      </div>
    </div>
  );
}
