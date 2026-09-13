import { useUploadOrganization } from './useUploadOrganization';

export default function UploadOrganization({ state, disabled }: {
  state: ReturnType<typeof useUploadOrganization>; disabled: boolean;
}) {
  return (
    <div className="mb-4">
      <label htmlFor="upload-organization" className="text-sm font-medium">Organization</label>
      <select id="upload-organization" value={state.organization}
        disabled={disabled || state.loading} required={state.required}
        onChange={event => state.setOrganization(event.target.value)}
        className="mt-1 w-full rounded-md border border-input bg-background p-2">
        <option value="">{state.loading ? 'Loading organizations…' : state.required ? 'Select an organization' : 'No organization assignment'}</option>
        {state.organizations.map(org => <option key={org.slug} value={org.slug}>{org.name}</option>)}
      </select>
      <p className="mt-1 text-xs text-muted-foreground">New patients will belong to the selected organization.</p>
      {state.error && <p role="alert" className="mt-2 text-sm text-destructive">{state.error}</p>}
    </div>
  );
}
