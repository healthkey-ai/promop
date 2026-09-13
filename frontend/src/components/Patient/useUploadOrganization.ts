import { useEffect, useState } from 'react';
import api from '@/api/axios';
import { useAuth } from '@/hooks/useAuth';

type Organization = { slug: string; name: string; is_active: boolean };

export function useUploadOrganization() {
  const { currentUser } = useAuth();
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [organization, setOrganization] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    api.get<Organization[]>('/orgs/').then(({ data }) => {
      if (cancelled) return;
      const active = data.filter(org => org.is_active);
      setOrganizations(active);
      if (active.length === 1) setOrganization(active[0].slug);
    }).catch(() => {
      if (!cancelled) setError('Unable to load organizations. Reload this page to try again.');
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);
  const required = !currentUser?.is_staff;
  return { organizations, organization, setOrganization, loading, error, required,
    ready: !loading && !error && (!required || !!organization) };
}

