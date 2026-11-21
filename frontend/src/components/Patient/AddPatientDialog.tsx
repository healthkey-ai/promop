import { useState } from "react";
import api from "@/api/axios";
import { useUploadOrganization } from "./useUploadOrganization";

interface AddPatientDialogProps {
  onClose: () => void;
  /** Called after the record exists; `forSelf` records are the admin's own. */
  onCreated: (result: { personId: number; forSelf: boolean; message: string }) => void;
}

type Mode = "invite" | "self" | "record";

/** Org admins create a patient record, optionally inviting the patient by email. */
export default function AddPatientDialog({ onClose, onCreated }: AddPatientDialogProps) {
  const org = useUploadOrganization();
  const [mode, setMode] = useState<Mode>("invite");
  const [givenName, setGivenName] = useState("");
  const [familyName, setFamilyName] = useState("");
  const [email, setEmail] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const needsEmail = mode === "invite";
  const hasName = givenName.trim() !== "" || familyName.trim() !== "";
  const canSubmit =
    !submitting && !org.loading && !org.error && (!org.required || !!org.organization) &&
    (mode === "self" ? true : needsEmail ? email.trim() !== "" : hasName || email.trim() !== "");

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const body: Record<string, unknown> = {
        given_name: givenName.trim(),
        family_name: familyName.trim(),
        email: email.trim(),
        invite: mode === "invite",
        for_self: mode === "self",
      };
      if (org.organization) body.organization = org.organization;
      const { data } = await api.post("/v1/patients/", body);
      const message = data?.email_warning
        ? data.email_warning
        : mode === "invite"
          ? `Invitation sent to ${email.trim().toLowerCase()}.`
          : mode === "self"
            ? "Your patient record was created."
            : "Patient record created.";
      onCreated({ personId: data.person_id, forSelf: mode === "self", message });
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { error?: string } } })?.response?.data?.error;
      setError(detail ?? "Could not create the patient record.");
      setSubmitting(false);
    }
  }

  const input = "mt-1 w-full rounded-md border border-input bg-background p-2 text-sm";
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <form
        role="dialog"
        aria-label="Add patient"
        onSubmit={handleSubmit}
        className="mx-4 w-full max-w-md rounded-2xl bg-background p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="mb-4 text-lg font-semibold text-foreground">Add patient</h3>

        <fieldset className="mb-4 space-y-1 text-sm">
          <legend className="sr-only">What to do</legend>
          <label className="flex items-center gap-2">
            <input type="radio" name="mode" checked={mode === "invite"} onChange={() => setMode("invite")} />
            Create the record and invite the patient by email
          </label>
          <label className="flex items-center gap-2">
            <input type="radio" name="mode" checked={mode === "record"} onChange={() => setMode("record")} />
            Create the record only
          </label>
          <label className="flex items-center gap-2">
            <input type="radio" name="mode" checked={mode === "self"} onChange={() => setMode("self")} />
            Create a record for myself
          </label>
        </fieldset>

        <label htmlFor="add-patient-organization" className="text-sm font-medium">Organization</label>
        <select
          id="add-patient-organization"
          value={org.organization}
          disabled={org.loading || submitting}
          required={org.required}
          onChange={(e) => org.setOrganization(e.target.value)}
          className={input}
        >
          <option value="">{org.loading ? "Loading organizations…" : "Select an organization"}</option>
          {org.organizations.map((o) => <option key={o.slug} value={o.slug}>{o.name}</option>)}
        </select>
        {org.error && <p role="alert" className="mt-2 text-sm text-destructive">{org.error}</p>}

        {mode !== "self" && (
          <>
            <div className="mt-3 grid grid-cols-2 gap-3">
              <div>
                <label htmlFor="add-patient-given" className="text-sm font-medium">First name</label>
                <input id="add-patient-given" value={givenName} onChange={(e) => setGivenName(e.target.value)} className={input} />
              </div>
              <div>
                <label htmlFor="add-patient-family" className="text-sm font-medium">Last name</label>
                <input id="add-patient-family" value={familyName} onChange={(e) => setFamilyName(e.target.value)} className={input} />
              </div>
            </div>
            <label htmlFor="add-patient-email" className="mt-3 block text-sm font-medium">
              Email{needsEmail ? "" : " (optional)"}
            </label>
            <input
              id="add-patient-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className={input}
              autoComplete="off"
            />
            {needsEmail && (
              <p className="mt-1 text-xs text-muted-foreground">
                The patient gets a link at this address to set their own password.
              </p>
            )}
          </>
        )}
        {mode === "self" && (
          <p className="mt-3 text-sm text-muted-foreground">
            A record linked to your own account. You keep your admin role and also get the patient role.
          </p>
        )}

        {error && <p role="alert" className="mt-3 text-sm text-destructive">{error}</p>}

        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onClose} className="rounded-md border border-input px-4 py-2 text-sm hover:bg-accent">
            Cancel
          </button>
          <button
            type="submit"
            disabled={!canSubmit}
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {submitting ? "Creating…" : mode === "invite" ? "Create and invite" : "Create"}
          </button>
        </div>
      </form>
    </div>
  );
}
