import PageTitle from '@/components/Branding/PageTitle';
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft, ChevronDown, ChevronRight, Download, Search, BookOpen, Check, X, Pencil, Plus, Sparkles } from "lucide-react";
import api from "@/api/axios";
import { fetchDiseaseCategories } from "@/api/mappingHub";
import { useAuth } from "@/hooks/useAuth";
import { invalidateAllWritableFields } from "@/hooks/useWritableFields";
import { ConceptAssignDialog } from "./ConceptAssignDialog";
import { SynonymDialog } from "./SynonymDialog";
import { FieldChoiceEditor } from "./FieldChoiceEditor";
import { FormulaEditDialog } from "./FormulaEditDialog";
import { DerivationInfoDialog } from "./DerivationInfoDialog";
import { addFieldReturn, type AddFieldReturn } from "@/components/PatientInfo/fieldMappingLinks";
import { canWriteMappings, isStaffOrOrgAdmin } from "@/utils/roles";
import { blobErrorMessage, filenameFromDisposition, saveBlob } from "@/utils/download";
import {
  BASE_FILTER, OTHER_DISEASE, canHaveScope, fieldDiseases, inDiseaseView, resolveDiseaseFilter,
} from "@/utils/fieldScope";
import type { DiseaseCategoryOption, DiseaseSet, FieldScoped } from "@/types/fieldScope";

interface FieldDescriptor extends FieldScoped {
  field_name: string;
  field_type: string;
  category: string;
  tab: string;
  provenance: {
    omop_table: string;
    lookup_strategy: string;
    concept_codes: string[] | null;
    source_values: string[] | null;
    extractor: string;
    selection_rule: string;
    description: string;
  } | null;
  mapping: {
    id: number;
    concept_id: number | null;
    concept_name: string;
    vocabulary_id: string;
    concept_code: string;
    unit: string;
    omop_table: string;
    status: string;
    /** Suggest engine and version, "curator", or the approving user (#1719). */
    provenance?: string;
    reviewer: string | null;
    reviewed_at: string | null;
    notes: string;
    /** Curator placement; blank keeps the code's, "omop" is unplaced (#2328). */
    tab?: string;
  } | null;
  suggestion: {
    concept_code: string;
    vocabulary_id: string | null;
    unit: string | null;
    omop_table: string;
    common_units: string[];
  } | null;
  /** Concept-search text the dialog's Suggest button seeds. */
  suggest_query?: string;
  mappable: boolean;
  locked_table: string | null;
  unit_options: string[];
  choices: {
    id: number;
    display: string;
    sort_order: number;
    codes: { code: string; vocabulary_id: string; display: string; is_primary: boolean }[];
  }[];
  formula: {
    id: number;
    expression: string;
    is_active: boolean;
  } | null;
  explanation: string | null;
  derivation_error: string | null;
}

const CATEGORY_LABELS: Record<string, string> = {
  "needs-concept-set": "Needs Concept Assignment",
  editable: "Mapped",
  computed: "Computed",
  profile: "Person",
};

const TAB_LABELS: Record<string, string> = {
  general: "General",
  disease: "Disease",
  treatment: "Treatment",
  blood: "Blood",
  labs: "Labs",
  genomics: "Genomics",
  behavior: "Behavior",
  wearable: "Wearable",
  // Unplaced: the field keeps its mapping and derivation but no patient tab
  // renders it, so its facts show only as raw OMOP rows (#2328).
  omop: "OMOP (unplaced)",
};

const TAB_ORDER = ["general", "disease", "treatment", "blood", "labs", "genomics", "behavior", "wearable", "omop"];

const STATUS_BADGE: Record<string, string> = {
  proposed: "bg-yellow-100 text-yellow-800",
  approved: "bg-green-100 text-green-800",
  rejected: "bg-red-100 text-red-800",
};

/**
 * Omit descriptors that cannot be administered here. Location persistence is
 * not supported end-to-end, while aliases only preserve older import names
 * and are not active PatientRecord fields with their own mappings.
 */
const isSupportedMapperField = (descriptor: FieldDescriptor) =>
  !["location", "alias"].includes(descriptor.category);

/** Display category: approved mappings move from their backend category to "editable" (Mapped). */
const getDisplayCategory = (d: FieldDescriptor): string => {
  if (d.category === "computed") return "computed";
  // Mapped is an approval ledger: every mappable field remains review work
  // until approved. This includes Person fields, which retain their Person
  // table lock in the dialog.
  if (d.mapping?.status === "approved") return "editable";
  if (d.mapping || d.mappable) return "needs-concept-set";
  return d.category;
};

/**
 * A `?set=` value from the page's own URL. The page opens a disease on its
 * own fields unless the full record is asked for; the list endpoint's
 * default is the full record.
 */
const diseaseSetParam = (value: string | null): DiseaseSet =>
  value === "profile" ? "profile" : "extension";

/**
 * The scope a new field's dialog opens on. A disease the categories do not
 * list is kept as written: the dialog shows it ticked and the server says
 * why it is refused, rather than the field silently becoming base.
 */
const newFieldDiseases = (disease: string, options: DiseaseCategoryOption[]): string[] => {
  if (!disease) return [];
  const resolved = resolveDiseaseFilter(disease, options);
  if (resolved === BASE_FILTER) return [];
  return [resolved ?? disease];
};

/**
 * A field being created (#721): the tab and scope its dialog opens on, and,
 * when a patient tab's Add field link opened it, where to go back to.
 */
interface NewField {
  tab: string;
  /** A Disease Category, as its code or as a link spelled it; "" is the base record. */
  disease: string;
  returnTo: AddFieldReturn | null;
}

/** The tabs a new field can be placed on, in the page's order. */
const TAB_OPTIONS = TAB_ORDER.map((value) => ({ value, label: TAB_LABELS[value] }));

/** Which download is in flight, for the button that started it. */
type Download = DiseaseSet | "base";

const DOWNLOAD_BUTTON =
  "inline-flex h-9 items-center gap-1.5 rounded border border-gray-300 bg-white px-3 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50";

/** Stored provenance as written; the two pre-#1719 labels get their old names. */
const provenanceLabel = (mapping: FieldDescriptor["mapping"]): string => {
  if (!mapping) return "—";
  if (mapping.provenance === "system_generated") return "System Generated";
  if (mapping.provenance === "curator") return "Curator";
  return mapping.provenance || "Unrecorded";
};

export default function FieldMappingPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { currentUser, loading: userLoading } = useAuth();
  const canApprove = isStaffOrOrgAdmin(currentUser);
  // A doctor without the analyst grant reads every mapping but writes none
  // (#2416): the API answers 403 on every write, so the page offers none.
  const canWrite = canWriteMappings(currentUser);
  const [descriptors, setDescriptors] = useState<FieldDescriptor[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  // ?field=<name>, from a patient-editor field's globe: open that field once,
  // on the first load. Later reloads (after a save) must not reopen it.
  const [searchParams, setSearchParams] = useSearchParams();
  const linkedField = useRef(searchParams.get("field"));
  // ?new=1&tab=<tab>[&disease=<code>], from a patient tab's Add field link
  // (#721): open the dialog in create mode on that tab and scope. Read once,
  // like ?field=; the link's router state says where to go back to.
  const [newField, setNewField] = useState<NewField | null>(() => {
    if (searchParams.get("new") !== "1") return null;
    const tab = searchParams.get("tab") || "";
    return {
      tab: TAB_ORDER.includes(tab) ? tab : "general",
      disease: (searchParams.get("disease") || "").trim(),
      returnTo: addFieldReturn(location.state),
    };
  });
  const [activeTab, setActiveTab] = useState(() => newField?.tab ?? "general");
  const [collapsedSections, setCollapsedSections] = useState<Set<string>>(
    new Set(["computed"])
  );
  const [dialogOpen, setDialogOpen] = useState(false);
  const [selectedField, setSelectedField] = useState<FieldDescriptor | null>(null);
  const [synonymDialogField, setSynonymDialogField] = useState<string | null>(null);
  const [batchSynonyms, setBatchSynonyms] = useState<Record<string, string[]>>({});
  const [choiceEditorField, setChoiceEditorField] = useState<FieldDescriptor | null>(null);
  const [formulaEditorField, setFormulaEditorField] = useState<FieldDescriptor | null>(null);
  const [derivationInfoField, setDerivationInfoField] = useState<FieldDescriptor | null>(null);
  // Whose fields are listed (#2332): every field, the base record, or one
  // Disease Category's; for a category, its disease-specific fields (the
  // extension) or its full record (base + extension). Applied to the
  // descriptors already loaded, so choosing a disease is no further request.
  // ?disease=<code>&set=extension|profile opens the page on that view; like
  // ?field=, it is read once and the filter is the page's own state after.
  const [diseaseChoice, setDiseaseChoice] = useState(() => (searchParams.get("disease") || "").trim());
  const [diseaseSet, setDiseaseSet] = useState<DiseaseSet>(() => diseaseSetParam(searchParams.get("set")));
  const [categories, setCategories] = useState<DiseaseCategoryOption[]>([]);
  const [categoriesState, setCategoriesState] = useState<"loading" | "loaded" | "failed">("loading");
  const [downloading, setDownloading] = useState<Download | null>(null);

  const fetchDescriptors = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const resp = await api.get("/v1/field-mappings/");
      const loaded: FieldDescriptor[] = resp.data.filter(isSupportedMapperField);
      setDescriptors(loaded);
      const wanted = linkedField.current;
      if (wanted) {
        linkedField.current = null;
        const field = loaded.find((d) => d.field_name === wanted);
        if (!field) {
          setError(`${wanted} has no field concept mapping entry.`);
        } else {
          setActiveTab(field.tab);
          if (field.mappable && canWrite) {
            setSelectedField(field);
            setDialogOpen(true);
          } else {
            // Nothing to assign (or nobody who may assign it); show the row
            // where it lives. Unmappable rows are mostly computed, whose
            // section starts collapsed.
            setSearchQuery(field.field_name);
            setCollapsedSections((previous) => {
              const next = new Set(previous);
              next.delete(getDisplayCategory(field));
              return next;
            });
          }
        }
      }
    } catch {
      setError("Failed to load field mappings.");
    } finally {
      setLoading(false);
    }
  }, [canWrite]);

  useEffect(() => {
    (async () => {
      await fetchDescriptors();
    })();
  }, [fetchDescriptors]);

  // Disease Category management's categories, read on every visit so one a
  // curator has just added is offered. The page works without them: the
  // filter then offers the categories the loaded fields already name.
  useEffect(() => {
    (async () => {
      try {
        const loaded: unknown = await fetchDiseaseCategories();
        if (!Array.isArray(loaded)) throw new Error("not a category list");
        setCategories(loaded.filter((c): c is DiseaseCategoryOption => (
          !!c && typeof c.value === "string" && typeof c.label === "string"
        )));
        setCategoriesState("loaded");
      } catch {
        // The scope control says so; the filter needs no more than the fields.
        setCategoriesState("failed");
      }
    })();
  }, []);

  // The categories a field can be scoped to: Disease Category management's,
  // and any a loaded field already names should that list not have loaded.
  // Other has no extension: its record is the base record.
  const diseaseOptions = useMemo(() => {
    const options = categories.filter((c) => c.value !== OTHER_DISEASE);
    const known = new Set(options.map((c) => c.value));
    const unlisted = [...new Set(descriptors.flatMap(fieldDiseases))]
      .filter((code) => !known.has(code)).sort();
    return [...options, ...unlisted.map((code) => ({ value: code, label: code }))];
  }, [categories, descriptors]);

  // The view the choice names. A URL may spell a category as the list
  // endpoint accepts it: any case, or by its label. One that names no
  // category is no filter, and the page says so, once the categories have
  // loaded: until then a category no field names yet cannot be told from a
  // mistyped one, so the value is kept as written.
  const resolvedChoice = resolveDiseaseFilter(diseaseChoice, diseaseOptions);
  const unknownDisease = resolvedChoice === null && categoriesState === "loaded" ? diseaseChoice : "";
  const diseaseFilter = resolvedChoice ?? (unknownDisease ? "" : diseaseChoice);

  // The select always has an option for what the list is filtered to.
  const filterOptions = useMemo(() => (
    !diseaseFilter || diseaseFilter === BASE_FILTER || diseaseOptions.some((c) => c.value === diseaseFilter)
      ? diseaseOptions
      : [...diseaseOptions, { value: diseaseFilter, label: diseaseFilter }]
  ), [diseaseOptions, diseaseFilter]);

  /** The fields the disease filter names, on every tab. */
  const inView = useMemo(
    () => descriptors.filter((d) => inDiseaseView(d, diseaseFilter, diseaseSet)),
    [descriptors, diseaseFilter, diseaseSet],
  );

  const tabCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const d of inView) {
      counts[d.tab] = (counts[d.tab] || 0) + 1;
    }
    return counts;
  }, [inView]);

  // The tab shown: the one chosen while it has a field in view, else the
  // first that does. A disease's extension can sit on a single tab, and the
  // chosen tab would otherwise read as an empty list.
  const shownTab = tabCounts[activeTab]
    ? activeTab
    : TAB_ORDER.find((tab) => tabCounts[tab]) ?? activeTab;

  const fetchBatchSynonyms = useCallback(async (fieldNames?: string[]) => {
    const names = fieldNames || descriptors
      .filter((d) => d.tab === shownTab)
      .map((d) => d.field_name);
    if (!names.length) return;
    try {
      const resp = await api.get(`/v1/field-synonyms/batch/?fields=${names.join(",")}`);
      setBatchSynonyms((prev) => ({ ...prev, ...resp.data }));
    } catch {
      // Silently fail — synonyms are non-critical.
    }
  }, [descriptors, shownTab]);

  // Load batch synonyms for current tab's fields.
  useEffect(() => {
    if (!descriptors.length) return;
    (async () => {
      await fetchBatchSynonyms();
    })();
  }, [descriptors, shownTab, fetchBatchSynonyms]);

  // When searching, show across all tabs; otherwise filter by active tab.
  const filtered = useMemo(() => {
    let items = inView;
    if (!searchQuery) {
      items = items.filter((d) => d.tab === shownTab);
    } else {
      const q = searchQuery.toLowerCase();
      items = items.filter((d) => d.field_name.toLowerCase().includes(q));
    }
    if (categoryFilter) {
      items = items.filter((d) => d.category === categoryFilter);
    }
    return items;
  }, [inView, categoryFilter, searchQuery, shownTab]);

  // Split into mappable and computed groups.
  const { mappableGroups, computedFields } = useMemo(() => {
    const mappable: Record<string, FieldDescriptor[]> = {};
    const computed: FieldDescriptor[] = [];
    for (const d of filtered) {
      const displayCat = getDisplayCategory(d);
      if (displayCat === "computed") {
        computed.push(d);
      } else {
        if (!mappable[displayCat]) mappable[displayCat] = [];
        mappable[displayCat].push(d);
      }
    }
    return { mappableGroups: mappable, computedFields: computed };
  }, [filtered]);

  const stats = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const d of inView) {
      const displayCat = getDisplayCategory(d);
      counts[displayCat] = (counts[displayCat] || 0) + 1;
    }
    return counts;
  }, [inView]);

  // The count beside each disease filter option: the base record's fields
  // and each category's extension.
  const scopeCounts = useMemo(() => {
    const counts: Record<string, number> = { [BASE_FILTER]: 0 };
    for (const d of descriptors) {
      const diseases = fieldDiseases(d);
      if (!diseases.length) counts[BASE_FILTER] += 1;
      for (const code of diseases) counts[code] = (counts[code] || 0) + 1;
    }
    return counts;
  }, [descriptors]);

  // Unmapped fields on the shown tab: in the view, for the count, and on the
  // whole tab, which is what Suggest proposes for whatever the disease filter.
  const { viewUnmapped, tabUnmapped } = useMemo(() => {
    const unmapped = descriptors.filter((d) =>
      d.tab === shownTab
      && d.mappable
      && !d.mapping
      && getDisplayCategory(d) !== "computed"
    );
    return {
      tabUnmapped: unmapped.length,
      viewUnmapped: unmapped.filter((d) => inDiseaseView(d, diseaseFilter, diseaseSet)).length,
    };
  }, [shownTab, descriptors, diseaseFilter, diseaseSet]);

  /** The Disease Category the filter names, or "" for All and Base record. */
  const selectedDisease = diseaseFilter && diseaseFilter !== BASE_FILTER ? diseaseFilter : "";

  /**
   * Leave create mode: back to the patient tab the link came from, which
   * reloads and shows a field just added, or on this page with the list
   * reloaded when the page's own button opened it.
   */
  const closeNewField = (created: boolean) => {
    const returnTo = newField?.returnTo;
    setNewField(null);
    // A new field is writable on every record it is part of; the patient
    // page this returns to must ask again rather than render it read-only.
    if (created) invalidateAllWritableFields();
    if (returnTo) {
      // Replacing this entry: Back from the patient page must not reopen
      // the dialog on a field already added or abandoned.
      navigate(returnTo.path, { state: { tab: returnTo.tab }, replace: true });
      return;
    }
    // Nowhere to go back to, so the link's request is spent: a reload must
    // not ask again. `disease` stays, as the page's own filter parameter.
    if (searchParams.has("new")) {
      setSearchParams((previous) => {
        const next = new URLSearchParams(previous);
        next.delete("new");
        next.delete("tab");
        return next;
      }, { replace: true });
    }
    if (created) void fetchDescriptors();
  };

  /**
   * Save one of the review CSVs (#2331): a disease's full record or its
   * disease-specific fields, or the base record when no disease is named.
   * The file is named by the server.
   */
  const handleDownload = async (which: Download) => {
    setDownloading(which);
    try {
      const params = which === "base" ? {} : { disease: selectedDisease, set: which };
      const response = await api.get<Blob>("/v1/field-mappings/export/", { params, responseType: "blob" });
      saveBlob(response.data, filenameFromDisposition(
        response.headers?.["content-disposition"],
        which === "base" ? "field-mappings-base.csv" : `field-mappings-${selectedDisease}-${which}.csv`,
      ));
      setError("");
    } catch (err) {
      setError((await blobErrorMessage(err)) ?? "Failed to download the field list.");
    } finally {
      setDownloading(null);
    }
  };

  const downloadButton = (which: Download, label: string, title: string) => (
    <button
      type="button"
      onClick={() => handleDownload(which)}
      disabled={downloading !== null}
      title={title}
      className={DOWNLOAD_BUTTON}
    >
      <Download size={14} />
      {downloading === which ? "Downloading…" : label}
    </button>
  );

  const toggleSection = (cat: string) => {
    setCollapsedSections((prev) => {
      const next = new Set(prev);
      if (next.has(cat)) next.delete(cat);
      else next.add(cat);
      return next;
    });
  };

  const handleCellClick = (field: FieldDescriptor) => {
    if (!field.mappable || !canWrite) return;
    setSelectedField(field);
    setDialogOpen(true);
  };

  const handleMappingSaved = () => {
    setDialogOpen(false);
    setSelectedField(null);
    // A mapping decides what a patient page may write.
    invalidateAllWritableFields();
    fetchDescriptors();
  };

  const handleDeleteMapping = async (mappingId: number) => {
    if (!window.confirm("Remove this concept mapping? This cannot be undone.")) return;
    try {
      await api.delete(`/v1/field-mappings/${mappingId}/`);
      fetchDescriptors();
    } catch {
      setError("Failed to remove mapping.");
    }
  };

  const handleConfirm = async (field: FieldDescriptor) => {
    try {
      if (field.mapping) {
        // This is a state toggle, not a one-way confirmation.  An approved
        // mapping is still retained as a proposal when a reviewer unchecks it.
        const status = field.mapping.status === "approved" ? "proposed" : "approved";
        await api.patch(`/v1/field-mappings/${field.mapping.id}/`, { status });
        await fetchDescriptors();
      } else if (field.suggestion) {
        // Suggestion has no resolved concept FK — open the dialog so the user
        // can search, select a concept, and create a complete mapping.
        handleCellClick(field);
      }
    } catch {
      setError("Failed to confirm mapping.");
    }
  };

  const handleSuggestCurrentTab = async () => {
    try {
      await api.post("/v1/field-mappings/propose-all/", { tab: shownTab });
      await fetchDescriptors();
    } catch {
      setError("Failed to suggest field mappings.");
    }
  };

  /** Whose record the field is part of: Base, or its Disease Category codes. */
  const renderScopeCell = (f: FieldDescriptor) => {
    const diseases = fieldDiseases(f);
    if (!diseases.length) {
      return <span className="text-xs text-gray-500" title="Part of every patient's record">Base</span>;
    }
    return (
      <span className="inline-flex flex-wrap gap-1" title={`Disease-specific: ${diseases.join(", ")}`}>
        {diseases.map((code) => (
          <span key={code} className="rounded bg-indigo-50 px-1.5 py-0.5 font-mono text-[11px] not-italic text-indigo-700">
            {code}
          </span>
        ))}
      </span>
    );
  };

  /** Render the concept cell content (code + status badge). */
  const renderConceptCell = (f: FieldDescriptor) => {
    if (f.mapping) {
      return (
        <span className="inline-flex items-center gap-1.5">
          <span className={`text-xs ${f.mapping.status === "proposed" ? "font-bold" : ""}`}>
            <span className="font-mono">{f.mapping.concept_code}</span>
            {f.mapping.concept_name && <span className="ml-1 break-words text-gray-600">— {f.mapping.concept_name}</span>}
          </span>
          <span
            className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
              STATUS_BADGE[f.mapping.status] || "bg-gray-100"
            }`}
          >
            {f.mapping.status}
          </span>
        </span>
      );
    }
    if (f.suggestion) {
      return (
        <span className="font-mono text-xs font-bold text-gray-700">
          {f.suggestion.concept_code}
        </span>
      );
    }
    return (
      <span className="text-xs text-gray-400 group-hover:text-gray-500">
        {canWrite ? "click to map" : "unmapped"}
      </span>
    );
  };

  /** Render coding (vocabulary_id) cell. */
  const renderCodingCell = (f: FieldDescriptor) => {
    if (f.mapping?.vocabulary_id) {
      return <span className="text-xs">{f.mapping.vocabulary_id}</span>;
    }
    return <span className="text-xs text-gray-400">&mdash;</span>;
  };

  /** Render table (omop_table) cell. */
  const renderTableCell = (f: FieldDescriptor) => {
    if (f.locked_table) {
      return (
        <span className="text-xs text-gray-400" title="Person table (locked)">
          {f.locked_table}
        </span>
      );
    }
    if (f.mapping?.omop_table) {
      return <span className="text-xs">{f.mapping.omop_table}</span>;
    }
    if (f.suggestion?.omop_table) {
      return <span className="text-xs italic text-gray-400">{f.suggestion.omop_table}</span>;
    }
    return <span className="text-xs text-gray-400">&mdash;</span>;
  };

  /** Render inline synonyms. */
  const renderSynonyms = (f: FieldDescriptor) => {
    const syns = batchSynonyms[f.field_name] || [];
    const content = (
      <>
        <BookOpen size={12} />
        {syns.length > 0 ? (
          <span className="max-w-[150px] truncate text-gray-500">
            {syns.slice(0, 3).join(", ")}
            {syns.length > 3 && <span className="ml-1 text-gray-400">+{syns.length - 3}</span>}
          </span>
        ) : (
          <span className="text-gray-400">none</span>
        )}
      </>
    );
    if (!canWrite) {
      return (
        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 text-xs text-gray-600">
          {content}
        </span>
      );
    }
    return (
      <button
        onClick={() => setSynonymDialogField(f.field_name)}
        className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs text-gray-600 hover:bg-gray-100"
        title="Manage synonyms"
      >
        {content}
      </button>
    );
  };

  /** Render table for a category section (mappable fields). */
  const renderMappableTable = (fields: FieldDescriptor[]) => (
    <div className="overflow-x-auto rounded-b-md border border-t-0 border-gray-200">
      <table className="min-w-full text-sm">
        <thead>
          <tr className="bg-gray-50 text-left text-[11px] uppercase text-gray-500">
            <th className="px-3 py-2">Provenance</th>
            <th className="px-3 py-2">Field Name</th>
            <th className="px-3 py-2">Scope</th>
            <th className="px-3 py-2">
              <span className="inline-flex items-center gap-1">
                Concept
                <span className="text-[9px] normal-case text-gray-400">Confirm</span>
              </span>
            </th>
            <th className="px-3 py-2">Coding</th>
            <th className="px-3 py-2">Table</th>
            <th className="px-3 py-2">Synonyms</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {fields.map((f) => {
            const clickable = f.mappable && canWrite;
            return (
            <tr key={f.field_name} className="group hover:bg-gray-50/50">
              <td className="px-3 py-2 text-xs whitespace-nowrap" title={f.mapping?.provenance || undefined}>
                {provenanceLabel(f.mapping)}
              </td>
              <td className="px-3 py-2 font-mono text-xs">{f.field_name}</td>
              <td className="px-3 py-2">{renderScopeCell(f)}</td>
              <td className="px-3 py-2">
                <div className="flex items-center gap-1.5">
                  {clickable && (
                    <button
                      onClick={() => handleConfirm(f)}
                      className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${
                        f.mapping?.status === "approved"
                          ? "border-green-500 bg-green-500 text-white"
                          : canApprove
                            ? "border-gray-300 hover:border-primary"
                            : "border-gray-200 bg-gray-100 cursor-not-allowed"
                      }`}
                      title={
                        !canApprove
                          ? "Only org admins and staff can approve mappings. Analysts may propose mappings for review."
                          : f.mapping?.status === "approved" ? "Mark mapping as proposed" : "Approve mapping"
                      }
                      disabled={(!f.mapping && !f.suggestion) || (!canApprove && f.mapping?.status !== "approved")}
                    >
                      {f.mapping?.status === "approved" && <Check size={10} />}
                    </button>
                  )}
                  {clickable ? (
                    <button
                      onClick={() => handleCellClick(f)}
                      className="inline-flex cursor-pointer items-center gap-1 hover:text-primary hover:underline"
                      title="Click to assign or edit concept"
                    >
                      {renderConceptCell(f)}
                      <Pencil size={10} className="opacity-0 group-hover:opacity-100 text-gray-400" />
                    </button>
                  ) : (
                    <span className="inline-flex items-center gap-1">{renderConceptCell(f)}</span>
                  )}
                  {canWrite && f.mapping && (
                    <button
                      onClick={() => handleDeleteMapping(f.mapping!.id)}
                      className="ml-1 rounded p-0.5 text-gray-400 hover:bg-red-50 hover:text-red-500"
                      title="Remove mapping"
                    >
                      <X size={10} />
                    </button>
                  )}
                </div>
              </td>
              <td className="px-3 py-2">
                {clickable ? (
                  <button onClick={() => handleCellClick(f)} className="cursor-pointer hover:text-primary">
                    {renderCodingCell(f)}
                  </button>
                ) : (
                  renderCodingCell(f)
                )}
              </td>
              <td className="px-3 py-2">
                {clickable && !f.locked_table ? (
                  <button onClick={() => handleCellClick(f)} className="cursor-pointer hover:text-primary">
                    {renderTableCell(f)}
                  </button>
                ) : (
                  renderTableCell(f)
                )}
              </td>
              <td className="px-3 py-2">{renderSynonyms(f)}</td>
            </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );

  /** Render computed fields section (read-only, at bottom). */
  const renderComputedSection = () => {
    if (computedFields.length === 0) return null;
    const isCollapsed = collapsedSections.has("computed");
    return (
      <div className="mb-3">
        <button
          onClick={() => toggleSection("computed")}
          className="flex w-full items-center gap-2 rounded bg-gray-50 px-3 py-2 text-left text-sm font-medium text-gray-500 hover:bg-gray-100"
        >
          {isCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
          Computed
          <span className="ml-1 text-xs text-gray-400">({computedFields.length})</span>
          <span className="ml-2 text-[10px] font-normal italic text-gray-400">
            read-only — computed by application code
          </span>
        </button>
        {!isCollapsed && (
          <div className="overflow-x-auto rounded-b-md border border-t-0 border-gray-200">
            <table className="min-w-full text-sm">
              <thead>
                <tr className="bg-gray-50 text-left text-[11px] uppercase text-gray-500">
                  <th className="px-3 py-2">Field Name</th>
                  <th className="px-3 py-2">Scope</th>
                  <th className="px-3 py-2">Type</th>
                  <th className="px-3 py-2">Formula / Explanation</th>
                  <th className="px-3 py-2">Provenance</th>
                  <th className="px-3 py-2">Derivation status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {computedFields.map((f) => (
                  <tr key={f.field_name} className="text-gray-400 italic">
                    <td className="px-3 py-2 font-mono text-xs">{f.field_name}</td>
                    <td className="px-3 py-2 not-italic">{renderScopeCell(f)}</td>
                    <td className="px-3 py-2 text-xs">{f.field_type}</td>
                    <td className="px-3 py-2 text-xs">
                      {f.formula ? (
                        canWrite ? (
                          <button
                            onClick={() => setFormulaEditorField(f)}
                            className="not-italic hover:text-primary hover:underline"
                          >
                            <span className="font-mono">{f.formula.expression}</span>
                          </button>
                        ) : (
                          <span className="font-mono not-italic">{f.formula.expression}</span>
                        )
                      ) : f.explanation ? (
                        <span className="not-italic text-gray-500">{f.explanation}</span>
                      ) : canWrite ? (
                        <button
                          onClick={() => setFormulaEditorField(f)}
                          className="not-italic hover:text-primary hover:underline"
                        >
                          <span className="text-gray-400">none</span>
                        </button>
                      ) : (
                        <span className="not-italic text-gray-400">none</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-xs">
                      <button
                        onClick={() => setDerivationInfoField(f)}
                        className="not-italic text-left hover:text-primary hover:underline"
                        title="View read-only derivation details"
                      >
                        {f.provenance ? (
                          <span title={f.provenance.description}>
                          {f.provenance.lookup_strategy} / {f.provenance.omop_table}
                          </span>
                        ) : "application code"}
                      </button>
                    </td>
                    <td className="px-3 py-2 text-xs not-italic">
                      {f.derivation_error ? (
                        <span className="font-medium text-red-600" title={f.derivation_error}>
                          Error in derivation
                        </span>
                      ) : (
                        <span className="text-gray-400">OK</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    );
  };

  if (loading) {
    return (
      <div className="mx-auto max-w-7xl p-6">
        <div className="space-y-3">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="h-8 animate-pulse rounded bg-gray-200" />
          ))}
        </div>
      </div>
    );
  }

  if (error && !descriptors.length) {
    return (
      <div className="mx-auto max-w-7xl p-6">
        <div className="rounded border border-red-300 bg-red-50 p-4 text-red-700">
          {error}
          <button onClick={() => fetchDescriptors()} className="ml-3 underline">Retry</button>
        </div>
      </div>
    );
  }

  const categoryOrder = Object.keys(CATEGORY_LABELS).filter((c) => c !== "computed");

  return (
    <div className="mx-auto max-w-7xl p-6">
      {/* Header */}
      <div className="mb-6 flex items-center gap-4">
        <button
          onClick={() => navigate("/")}
          className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft size={14} />
          Back
        </button>
        <PageTitle className="text-xl font-semibold">Field Concept Mappings</PageTitle>
        {canApprove && (
          <button
            onClick={() => setNewField({ tab: shownTab, disease: selectedDisease, returnTo: null })}
            className="ml-auto inline-flex items-center gap-1 rounded border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50"
          >
            <Plus size={15} /> Add field
          </button>
        )}
      </div>

      {newField && !userLoading && !canApprove && (
        <div role="alert" className="mb-4 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
          Only org admins and staff can add a field to PatientRecord.
        </div>
      )}

      {!canWrite && (
        <p role="status" className="mb-4 text-sm text-gray-600">Read-only: mapping codes needs the analyst role.</p>
      )}

      {/* Tab bar */}
      <div className="mb-4 flex gap-0 overflow-x-auto border-b border-gray-200">
        {TAB_ORDER.map((tab) => {
          const count = tabCounts[tab] || 0;
          if (!count) return null;
          return (
            <button
              key={tab}
              onClick={() => { setActiveTab(tab); setSearchQuery(""); }}
              className={`whitespace-nowrap border-b-2 px-4 py-2 text-sm font-medium transition-colors ${
                shownTab === tab && !searchQuery
                  ? "border-primary text-primary"
                  : "border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700"
              }`}
            >
              {TAB_LABELS[tab] || tab}
              <span className="ml-1.5 text-xs text-gray-400">({count})</span>
            </button>
          );
        })}
      </div>

      {/* Stats bar */}
      <div className="mb-4 flex flex-wrap gap-2 text-xs">
        {categoryOrder.map((cat) =>
          stats[cat] ? (
            <span
              key={cat}
              className={`rounded-full px-2.5 py-1 font-medium ${
                cat === "needs-concept-set"
                  ? "bg-amber-100 text-amber-800"
                  : "bg-gray-100 text-gray-600"
              }`}
            >
              {CATEGORY_LABELS[cat] || cat}: {stats[cat]}
            </span>
          ) : null
        )}
        {stats["computed"] ? (
          <span className="rounded-full bg-gray-100 px-2.5 py-1 font-medium text-gray-500 italic">
            Computed: {stats["computed"]}
          </span>
        ) : null}
        <span className="rounded-full bg-blue-100 px-2.5 py-1 font-medium text-blue-800">
          Total: {inView.length}
        </span>
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-3 text-sm text-gray-600">
        <span className="rounded-full bg-amber-100 px-2.5 py-1 text-xs font-medium text-amber-800">
          Unmapped Fields: {viewUnmapped}
        </span>
        {canWrite && (
          <button
            type="button"
            onClick={handleSuggestCurrentTab}
            disabled={tabUnmapped === 0 || inView.length === 0}
            title={diseaseFilter ? `Suggests for all ${tabUnmapped} unmapped fields on this tab, whatever their scope` : undefined}
            className="inline-flex items-center gap-2 rounded border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          >
            <Sparkles size={15} />
            Suggest
          </button>
        )}
      </div>

      {/* Filter bar */}
      <div className="mb-4 flex flex-wrap gap-3">
        <div className="relative">
          <Search size={14} className="absolute left-2.5 top-2.5 text-gray-400" />
          <input
            type="text"
            placeholder="Search fields (all tabs)..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="h-9 w-64 rounded border border-gray-300 pl-8 pr-3 text-sm focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>
        <select
          value={categoryFilter}
          onChange={(e) => setCategoryFilter(e.target.value)}
          className="h-9 rounded border border-gray-300 px-3 text-sm focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
        >
          <option value="">All categories</option>
          {Object.entries(CATEGORY_LABELS).map(([cat, label]) => (
            <option key={cat} value={cat}>
              {label} ({stats[cat] || 0})
            </option>
          ))}
        </select>
        <select
          aria-label="Disease"
          value={diseaseFilter}
          onChange={(e) => setDiseaseChoice(e.target.value)}
          className="h-9 max-w-full rounded border border-gray-300 px-3 text-sm focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
        >
          <option value="">All diseases ({descriptors.length})</option>
          <option value={BASE_FILTER}>Base record ({scopeCounts[BASE_FILTER]})</option>
          {filterOptions.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label === option.value ? option.value : `${option.value} — ${option.label}`} ({scopeCounts[option.value] || 0})
            </option>
          ))}
        </select>
        {selectedDisease && (
          <div role="group" aria-label="Fields shown" className="inline-flex h-9 overflow-hidden rounded border border-gray-300 text-sm">
            {([["extension", "Disease-specific"], ["profile", "Full record"]] as const).map(([value, label]) => (
              <button
                key={value}
                type="button"
                aria-pressed={diseaseSet === value}
                onClick={() => setDiseaseSet(value)}
                className={`px-3 font-medium focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-current ${
                  diseaseSet === value ? "bg-primary text-primary-foreground" : "bg-white text-gray-700 hover:bg-gray-50"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        )}
        {/* Review copies for a curator working offline; nothing loads them back. */}
        <div className="flex flex-wrap items-center gap-2 sm:ml-auto" aria-busy={downloading !== null}>
          {selectedDisease ? (
            <>
              {downloadButton(
                "profile", "Full record (CSV)",
                `Every field of a ${selectedDisease} patient's record with its concept: the base record plus ${selectedDisease}'s own fields`,
              )}
              {downloadButton(
                "extension", "Disease-specific (CSV)",
                `Only ${selectedDisease}'s disease-specific fields with their concepts`,
              )}
            </>
          ) : downloadButton(
            "base", "Base record (CSV)",
            "The fields every patient has, with their concepts. Choose a disease to download its record.",
          )}
        </div>
      </div>

      {/* Disease view indicator: always mounted, so a change is announced. */}
      <div
        role="status"
        aria-label="Disease view"
        className={diseaseFilter || unknownDisease ? "mb-3 text-sm text-gray-500" : "sr-only"}
      >
        {unknownDisease
          ? `"${unknownDisease}" is not a Disease Category, so every field is listed.`
          : diseaseFilter === BASE_FILTER
            ? `Showing the base record: ${inView.length} fields.`
            : selectedDisease && diseaseSet === "extension"
              ? `Showing ${selectedDisease}'s disease-specific fields (${inView.length}).`
              : selectedDisease
                ? `Showing ${selectedDisease}'s full record: the base record plus its disease-specific fields (${inView.length}).`
                : ""}
      </div>

      {/* Error banner (non-fatal) */}
      {error && (
        <div className="mb-4 rounded border border-red-300 bg-red-50 p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {/* Search mode indicator */}
      {searchQuery && (
        <div className="mb-3 text-sm text-gray-500">
          Showing results across all tabs for &quot;{searchQuery}&quot; ({filtered.length} fields)
        </div>
      )}

      {/* Mappable category sections */}
      {categoryOrder
        .filter((cat) => mappableGroups[cat]?.length)
        .map((cat) => {
          const isCollapsed = collapsedSections.has(cat);
          const fields = mappableGroups[cat];
          return (
            <div key={cat} className="mb-3">
              <button
                onClick={() => toggleSection(cat)}
                className={`flex w-full items-center gap-2 rounded px-3 py-2 text-left text-sm font-medium hover:bg-gray-100 ${
                  cat === "needs-concept-set"
                    ? "bg-amber-50 text-amber-800"
                    : "bg-gray-50 text-gray-700"
                }`}
              >
                {isCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
                {CATEGORY_LABELS[cat] || cat}
                <span className="ml-1 text-xs text-gray-400">({fields.length})</span>
              </button>
              {!isCollapsed && renderMappableTable(fields)}
            </div>
          );
        })}

      {/* Computed section at bottom */}
      {renderComputedSection()}

      {/* No results */}
      {Object.keys(mappableGroups).length === 0 && computedFields.length === 0 && (
        <div className="py-8 text-center text-sm text-gray-400">
          No fields match the current filters.
        </div>
      )}

      {/* Concept Assign Dialog */}
      {dialogOpen && selectedField && canWrite && (
        <ConceptAssignDialog
          fieldName={selectedField.field_name}
          fieldType={selectedField.field_type}
          suggestQuery={selectedField.suggest_query}
          onShowSourceCodes={(conceptId) => navigate(`/code-mappings?direction=reverse&concept=${conceptId}`)}
          initialConceptCode={selectedField.mapping?.concept_code || selectedField.suggestion?.concept_code}
          initialVocabularyId={selectedField.mapping?.vocabulary_id || (selectedField.suggestion?.vocabulary_id ?? undefined)}
          initialUnit={selectedField.mapping?.unit || (selectedField.suggestion?.unit ?? undefined)}
          initialOmopTable={selectedField.locked_table || selectedField.mapping?.omop_table || selectedField.suggestion?.omop_table || undefined}
          existingMappingId={selectedField.mapping?.id}
          initialConceptId={selectedField.mapping?.concept_id}
          initialConceptName={selectedField.mapping?.concept_name}
          initialStatus={selectedField.mapping?.status as "proposed" | "approved" | "rejected" | undefined}
          initialNotes={selectedField.mapping?.notes}
          canApprove={canApprove}
          storedUnit={selectedField.mapping?.unit}
          storedOmopTable={selectedField.mapping?.omop_table}
          fieldScope={canHaveScope(selectedField.field_name) ? {
            diseases: fieldDiseases(selectedField),
            options: diseaseOptions,
            optionsIncomplete: categoriesState === "failed",
          } : undefined}
          commonUnits={selectedField.unit_options}
          choices={selectedField.choices}
          onEditChoices={() => {
            const fieldToEdit = selectedField;
            setDialogOpen(false);
            setSelectedField(null);
            setChoiceEditorField(fieldToEdit);
          }}
          onClose={() => {
            setDialogOpen(false);
            setSelectedField(null);
          }}
          onSaved={handleMappingSaved}
        />
      )}

      {/* The same dialog in create mode. A scope names a category by its
          code, so one a link spelled is resolved once the categories load. */}
      {newField && canApprove && categoriesState !== "loading" && (
        <ConceptAssignDialog
          fieldName=""
          fieldType=""
          create={{ tab: newField.tab, tabOptions: TAB_OPTIONS }}
          fieldScope={{
            diseases: newFieldDiseases(newField.disease, diseaseOptions),
            options: diseaseOptions,
            optionsIncomplete: categoriesState === "failed",
          }}
          canApprove={canApprove}
          onClose={() => closeNewField(false)}
          onSaved={() => closeNewField(true)}
        />
      )}

      {/* Synonym Dialog */}
      {synonymDialogField && canWrite && (
        <SynonymDialog
          fieldName={synonymDialogField}
          onClose={() => {
            const fieldName = synonymDialogField;
            setSynonymDialogField(null);
            fetchBatchSynonyms([fieldName]);
          }}
        />
      )}

      {/* Field Choice Editor */}
      {choiceEditorField && canWrite && (
        <FieldChoiceEditor
          fieldName={choiceEditorField.field_name}
          canApprove={canApprove}
          diseaseOptions={diseaseOptions}
          diseaseOptionsIncomplete={categoriesState === "failed"}
          onClose={() => {
            setChoiceEditorField(null);
            fetchDescriptors();
          }}
        />
      )}

      {/* Formula Edit Dialog */}
      {formulaEditorField && canWrite && (
        <FormulaEditDialog
          fieldName={formulaEditorField.field_name}
          fieldType={formulaEditorField.field_type}
          existingFormula={formulaEditorField.formula}
          onClose={() => {
            setFormulaEditorField(null);
            fetchDescriptors();
          }}
        />
      )}

      {derivationInfoField && (
        <DerivationInfoDialog
          fieldName={derivationInfoField.field_name}
          provenance={derivationInfoField.provenance}
          onClose={() => setDerivationInfoField(null)}
        />
      )}
    </div>
  );
}
