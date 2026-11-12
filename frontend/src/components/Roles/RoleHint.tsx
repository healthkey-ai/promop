import { useState } from 'react';
import { Link } from 'react-router-dom';

import { ROLE_BY_KEY, ROLES_PAGE_PATH } from '@/utils/roleDescriptions';

/** A role name with a hover tooltip saying what the role can do and a link
 *  to the roles page (#2473). Unknown roles render their key, no tooltip. */
export default function RoleHint({ role, className = '' }: { role: string; className?: string }) {
  const [visible, setVisible] = useState(false);
  const description = ROLE_BY_KEY[role];
  if (!description) return <span className={className}>{role}</span>;
  return (
    <span
      className={`relative inline-flex items-center gap-1 ${className}`}
      onMouseEnter={() => setVisible(true)}
      onMouseLeave={() => setVisible(false)}
      onFocus={() => setVisible(true)}
      onBlur={() => setVisible(false)}
    >
      <span title={description.summary}>{description.label}</span>
      <Link
        to={ROLES_PAGE_PATH}
        aria-label={`What a ${description.label} can do`}
        className="text-blue-400 text-xs select-none hover:text-blue-600"
        onClick={(e) => e.stopPropagation()}
      >
        ⓘ
      </Link>
      {visible && (
        <span role="tooltip" className="absolute left-0 top-6 z-50 w-72 rounded bg-gray-800 p-2 text-xs font-normal text-white shadow-lg whitespace-normal">
          <span className="block font-semibold">{description.label}</span>
          <span className="block">{description.summary}</span>
          <span className="mt-1 block text-blue-300">All roles →</span>
        </span>
      )}
    </span>
  );
}
