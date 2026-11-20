import { useId, useRef, useState } from 'react';
import { Link } from 'react-router-dom';

import { ROLE_BY_KEY, ROLES_PAGE_PATH } from '@/utils/roleDescriptions';

/** A role name with a tooltip saying what the role can do, opened on hover
 *  or on focusing the ⓘ link, which goes to the roles page (#2473). The
 *  popup is fixed-positioned from the trigger so a table's scroll container
 *  cannot clip it. `hideLabel` keeps only the ⓘ, beside a control that
 *  already names the role. Unknown roles render their key, no tooltip. */
export default function RoleHint({ role, className = '', hideLabel = false }: { role: string; className?: string; hideLabel?: boolean }) {
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);
  const trigger = useRef<HTMLSpanElement>(null);
  const id = useId();
  const description = ROLE_BY_KEY[role];
  if (!description) return <span className={className}>{role}</span>;
  const open = () => {
    const rect = trigger.current?.getBoundingClientRect();
    setPosition(rect ? { top: rect.bottom + 4, left: rect.left } : { top: 0, left: 0 });
  };
  const close = () => setPosition(null);
  return (
    <span
      ref={trigger}
      className={`inline-flex items-center gap-1 ${className}`}
      onMouseEnter={open}
      onMouseLeave={close}
      onFocus={open}
      onBlur={close}
      onKeyDown={(e) => { if (e.key === 'Escape') close(); }}
    >
      {!hideLabel && <span>{description.label}</span>}
      <Link
        to={ROLES_PAGE_PATH}
        aria-label={`What the ${description.label} role can do`}
        aria-describedby={position ? id : undefined}
        className="text-blue-400 text-xs select-none hover:text-blue-600"
      >
        ⓘ
      </Link>
      {position && (
        <span id={id} role="tooltip" style={{ position: 'fixed', top: position.top, left: position.left }}
              className="z-50 w-72 rounded bg-gray-800 p-2 text-xs font-normal text-white shadow-lg whitespace-normal">
          <span className="block font-semibold">{description.label}</span>
          <span className="block">{description.summary}</span>
          <span className="mt-1 block text-gray-300">Click ⓘ for every role.</span>
        </span>
      )}
    </span>
  );
}
