import { useState, useEffect, useCallback } from "react";
import { AlertCircle, Syringe } from "lucide-react";
import type { User } from "@/hooks/useAuth";
import api from "@/api/axios";
import { formatDate } from "@/utils/date";

interface Immunization {
  drug_exposure_id: number;
  vaccine_name: string;
  date: string | null;
  lot_number: string | null;
}

export default function ImmunizationList({ user }: { user: User | null }) {
  const [immunizations, setImmunizations] = useState<Immunization[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchImmunizations = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get("/v1/immunizations/", {
        params: { person_id: user!.person_id },
      });
      setImmunizations(Array.isArray(res.data) ? res.data : res.data.results ?? []);
    } catch {
      setError("Failed to load immunization records.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    (async () => {
      if (user && user.person_id != null) {
        await fetchImmunizations();
      } else {
        setLoading(false);
      }
    })();
  }, [user, fetchImmunizations]);

  if (!user || user.person_id == null) return null;

  if (loading) {
    return (
      <div className="rounded-2xl bg-background p-8 shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
        <div className="flex items-center gap-3 mb-6">
          <div className="h-5 w-5 animate-pulse rounded bg-muted" />
          <div className="h-5 w-40 animate-pulse rounded bg-muted" />
        </div>
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-12 animate-pulse rounded-lg bg-muted" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-2xl bg-background p-8 shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
      <div className="flex items-center gap-2 mb-1">
        <Syringe className="h-5 w-5 text-portal-brand" />
        <h2 className="text-xl font-bold text-foreground">Immunizations</h2>
      </div>
      <p className="mb-6 text-sm text-muted-foreground">
        Your vaccination history from linked health records.
      </p>

      {error && (
        <div className="mb-4 flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3">
          <AlertCircle className="h-4 w-4 shrink-0 text-red-500" />
          <p className="text-sm text-red-700">{error}</p>
        </div>
      )}

      {immunizations.length === 0 && !error && (
        <p className="text-sm text-muted-foreground">No immunization records found.</p>
      )}

      {immunizations.length > 0 && (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/50">
                <th className="px-4 py-2.5 text-left font-medium text-muted-foreground">Vaccine</th>
                <th className="px-4 py-2.5 text-left font-medium text-muted-foreground">Date</th>
                <th className="px-4 py-2.5 text-left font-medium text-muted-foreground">Lot #</th>
              </tr>
            </thead>
            <tbody>
              {immunizations.map((imm) => (
                <tr key={imm.drug_exposure_id} className="border-b border-border last:border-0 hover:bg-muted/30">
                  <td className="px-4 py-3 font-medium text-foreground">{imm.vaccine_name}</td>
                  <td className="px-4 py-3 text-muted-foreground">{formatDate(imm.date)}</td>
                  <td className="px-4 py-3 text-muted-foreground">{imm.lot_number || "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
