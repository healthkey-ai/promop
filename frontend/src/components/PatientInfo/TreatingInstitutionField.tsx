import { useEffect, useId, useState } from 'react';
import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';
import { Input } from '@/components/shadcn/input';
import type { FieldDescriptor } from '@/hooks/useWritableFields';

interface Institution { id: string; label: string; state_code: string }
interface Props {
  value: string;
  descriptor?: FieldDescriptor;
  onChange: (name: string, value: unknown) => void;
}

export default function TreatingInstitutionField({ value, descriptor, onChange }: Props) {
  const id = useId();
  const [centers, setCenters] = useState<Institution[]>([]);
  const [unavailable, setUnavailable] = useState(false);
  const [draft, setDraft] = useState<string | null>(null);
  const query = draft ?? value;
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const writable = !!descriptor?.writable;
  useEffect(() => {
    let canceled = false;
    clinicalClient().get(clinicalUrl('/treating-institutions/')).then(({ data }) => {
      if (!Array.isArray(data?.institutions) || !data.institutions.every((c: Institution) =>
        c && typeof c.id === 'string' && typeof c.label === 'string' && typeof c.state_code === 'string')) {
        throw new Error('Invalid institution directory');
      }
      if (!canceled) setCenters(data.institutions);
    }).catch(() => { if (!canceled) setUnavailable(true); });
    return () => { canceled = true; };
  }, []);

  const normalized = query.trim().toLocaleLowerCase();
  const matches = centers.filter(c => `${c.label} ${c.state_code}`.toLocaleLowerCase().includes(normalized));
  const options = matches.map(c => ({ key: c.id, value: c.label, text: c.label }));
  if (query.trim() && !centers.some(c => c.label === query.trim())) {
    options.push({ key: 'custom', value: query.trim(), text: `Use entered name: ${query.trim()}` });
  }
  const choose = (next: string) => {
    if (!writable) return;
    setDraft(null);
    setOpen(false);
    setActive(-1);
    onChange('facility_name', next);
  };
  return (
    <div className="relative space-y-1.5">
      <label htmlFor={id} className="text-sm font-medium text-portal-text-primary">Treating Institution</label>
      <div className="flex gap-2">
        <Input id={id} role="combobox" aria-autocomplete="list" aria-expanded={open && writable}
          aria-controls={`${id}-list`} aria-activedescendant={open && active >= 0 ? `${id}-option-${active}` : undefined}
          aria-describedby={`${id}-help`} autoComplete="off" value={query} readOnly={!writable} maxLength={255}
          placeholder="Search center name, city, or state"
          onFocus={() => { if (writable) setOpen(true); }}
          onBlur={() => { setOpen(false); setActive(-1); setDraft(null); }}
          onChange={e => { setDraft(e.target.value); setActive(-1); setOpen(true); }}
          onKeyDown={e => {
            if (!writable) return;
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
              e.preventDefault(); setOpen(true);
              setActive(i => Math.max(0, Math.min(options.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1))));
            } else if (e.key === 'Enter' && open) {
              e.preventDefault(); choose(active >= 0 && options[active] ? options[active].value : query.trim());
            } else if (e.key === 'Escape') {
              e.preventDefault(); setOpen(false); setActive(-1); setDraft(null);
            }
          }} />
        {writable && value && <button type="button" className="text-sm text-muted-foreground" onClick={() => choose('')} aria-label="Clear treating institution">Clear</button>}
      </div>
      {open && writable && options.length > 0 && (
        <ul id={`${id}-list`} role="listbox" aria-label="Treating institutions" className="absolute z-30 max-h-60 w-full overflow-auto rounded-md border bg-white py-1 shadow-md">
          {options.map((option, index) => (
            <li id={`${id}-option-${index}`} key={option.key} role="option" aria-selected={active === index}
              className={`cursor-pointer px-3 py-2 text-sm ${active === index ? 'bg-slate-100' : 'hover:bg-slate-50'}`}
              ref={node => { if (node && active === index) node.scrollIntoView?.({ block: 'nearest' }); }}
              onMouseDown={e => e.preventDefault()} onClick={() => choose(option.value)}>{option.text}</li>
          ))}
        </ul>
      )}
      <p id={`${id}-help`} className="text-xs text-muted-foreground">
        {!writable ? descriptor?.reason || 'You do not have permission to edit this field.'
          : unavailable ? 'Directory unavailable. Enter a center name and press Enter to use it.'
            : <>Search the <a href="https://www.cancer.gov/research/infrastructure/cancer-centers/find" target="_blank" rel="noreferrer" className="underline">NCI cancer-center directory</a>, or enter another center and press Enter.</>}
      </p>
    </div>
  );
}
