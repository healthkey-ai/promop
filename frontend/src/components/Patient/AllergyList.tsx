import { useState, useEffect, useCallback } from "react";
import { AlertCircle, ShieldAlert } from "lucide-react";
import type { User } from "@/hooks/useAuth";
import api from "@/api/axios";
import { formatDate } from "@/utils/date";

interface Allergy {
  observation_id: number;
  allergen_name: string;
  criticality: string;
  clinical_status: string;
  recorded_date: string | null;
}

const CRITICALITY_STYLES: Record<string, string> = {
  high: "bg-red-100 text-red-700",
  low: "bg-yellow-100 text-yellow-700",
  "unable-to-assess": "bg-gray-100 text-gray-700",
};

export default function AllergyList({ user }: { user: User | null }) {
  const [allergies, setAllergies] = useState<Allergy[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchAllergies = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get("/v1/allergies/", {
        params: { person_id: user!.person_id },
      });
      setAllergies(Array.isArray(res.data) ? res.data : res.data.results ?? []);
    } catch {
      setError("Failed to load allergy records.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    (async () => {
      if (user && user.person_id != null) {
        await fetchAllergies();
      } else {
        setLoading(false);
      }
    })();
  }, [user, fetchAllergies]);

  if (!user || user.person_id == null) return null;

  if (loading) {
    return (
      <div className="rounded-2xl bg-background p-8 shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
        <div className="flex items-center gap-3 mb-6">
          <div className="h-5 w-5 animate-pulse rounded bg-muted" />
          <div className="h-5 w-40 animate-pulse rounded bg-muted" />
        </div>
        <div className="space-y-3">
          {[1, 2].map((i) => (
            <div key={i} className="h-12 animate-pulse rounded-lg bg-muted" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-2xl bg-background p-8 shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
      <div className="flex items-center gap-2 mb-1">
        <ShieldAlert className="h-5 w-5 text-portal-brand" />
        <h2 className="text-xl font-bold text-foreground">Allergies</h2>
      </div>
      <p className="mb-6 text-sm text-muted-foreground">
        Known allergies and intolerances from your health records.
      </p>

      {error && (
        <div className="mb-4 flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3">
          <AlertCircle className="h-4 w-4 shrink-0 text-red-500" />
          <p className="text-sm text-red-700">{error}</p>
        </div>
      )}

      {allergies.length === 0 && !error && (
        <p className="text-sm text-muted-foreground">No allergy records found.</p>
      )}

      {allergies.length > 0 && (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/50">
                <th className="px-4 py-2.5 text-left font-medium text-muted-foreground">Allergen</th>
                <th className="px-4 py-2.5 text-left font-medium text-muted-foreground">Criticality</th>
                <th className="px-4 py-2.5 text-left font-medium text-muted-foreground">Status</th>
                <th className="px-4 py-2.5 text-left font-medium text-muted-foreground">Recorded</th>
              </tr>
            </thead>
            <tbody>
              {allergies.map((allergy) => (
                <tr key={allergy.observation_id} className="border-b border-border last:border-0 hover:bg-muted/30">
                  <td className="px-4 py-3 font-medium text-foreground">{allergy.allergen_name}</td>
                  <td className="px-4 py-3">
                    {allergy.criticality ? (
                      <span
                        className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-medium ${
                          CRITICALITY_STYLES[allergy.criticality.toLowerCase()] ?? "bg-gray-100 text-gray-700"
                        }`}
                      >
                        {allergy.criticality}
                      </span>
                    ) : (
                      <span className="text-muted-foreground">-</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">{allergy.clinical_status || "-"}</td>
                  <td className="px-4 py-3 text-muted-foreground">{formatDate(allergy.recorded_date)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
