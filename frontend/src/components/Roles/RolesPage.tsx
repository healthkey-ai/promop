import PageTitle from '@/components/Branding/PageTitle';
import { ArrowLeft } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

import { ROLE_DESCRIPTIONS } from '@/utils/roleDescriptions';

/** What each role can and cannot do (#2473). Linked from the Access Grants
 *  tab and from every role tooltip. */
export default function RolesPage() {
  const navigate = useNavigate();
  return (
    <div className="p-6 space-y-6 max-w-3xl">
      <div className="flex items-center gap-3">
        <button type="button" onClick={() => navigate(-1)} className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-800">
          <ArrowLeft size={14} /> Back
        </button>
        <PageTitle>Roles</PageTitle>
      </div>
      <p className="text-sm text-gray-600">
        Roles are additive. A user holds one grant per role at an organization, and each grant adds its own reach:
        a doctor who should also work on mappings holds both the Doctor and the Analyst role. An org admin needs
        no other role. Staff accounts reach everything on every organization.
      </p>
      {ROLE_DESCRIPTIONS.map((role) => (
        <section key={role.key} aria-labelledby={`role-${role.key}`} className="rounded-lg border border-gray-200 p-4 space-y-3">
          <h2 id={`role-${role.key}`} className="font-medium text-gray-900">{role.label}</h2>
          <p className="text-sm text-gray-700">{role.summary}</p>
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500">Can</h3>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-gray-700">
                {role.can.map((item) => <li key={item}>{item}</li>)}
              </ul>
            </div>
            <div>
              <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500">Cannot</h3>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-gray-700">
                {role.cannot.map((item) => <li key={item}>{item}</li>)}
              </ul>
            </div>
          </div>
        </section>
      ))}
    </div>
  );
}
