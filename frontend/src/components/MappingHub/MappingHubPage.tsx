import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, Database, Braces, FlaskConical } from 'lucide-react';
import { fetchMappingStats, type MappingStats } from '@/api/mappingHub';

export default function MappingHubPage() {
  const navigate = useNavigate();
  const [stats, setStats] = useState<MappingStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchMappingStats();
      setStats(data);
    } catch {
      setError('Failed to load mapping statistics.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    (async () => {
      await load();
    })();
  }, [load]);

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="mb-6 flex items-center gap-3">
        <button
          onClick={() => navigate('/')}
          className="rounded p-1 text-muted-foreground hover:bg-accent"
          aria-label="Back to patients"
        >
          <ArrowLeft size={20} />
        </button>
        <h1 className="text-2xl font-bold text-foreground">Mapping Administration</h1>
      </div>

      {error && (
        <div className="mb-4 rounded-md bg-destructive/10 p-4 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {/* Field Mapping Card */}
        <button
          onClick={() => navigate('/field-mappings')}
          className="rounded-lg border border-border bg-background p-6 shadow-sm text-left hover:border-primary/50 hover:shadow-md transition-all"
        >
          <div className="flex items-center gap-3 mb-4">
            <div className="rounded-lg bg-blue-50 p-2.5 text-blue-600">
              <Database size={22} />
            </div>
            <h2 className="text-lg font-semibold text-foreground">Field Mapping</h2>
          </div>
          {loading ? (
            <div className="text-sm text-muted-foreground">Loading...</div>
          ) : stats ? (
            <div className="space-y-1 text-sm text-muted-foreground">
              <div>{stats.field_mappings.total} total fields</div>
              <div className="text-green-600">{stats.field_mappings.approved} approved</div>
              <div className="text-amber-600">{stats.field_mappings.proposed} proposed</div>
              <div className="text-gray-400">{stats.field_mappings.unmapped} unmapped</div>
            </div>
          ) : null}
        </button>

        {/* Code Mapping Card */}
        <button
          onClick={() => navigate('/code-mappings')}
          className="rounded-lg border border-border bg-background p-6 shadow-sm text-left hover:border-primary/50 hover:shadow-md transition-all"
        >
          <div className="flex items-center gap-3 mb-4">
            <div className="rounded-lg bg-purple-50 p-2.5 text-purple-600">
              <Braces size={22} />
            </div>
            <h2 className="text-lg font-semibold text-foreground">Code Mapping</h2>
          </div>
          {loading ? (
            <div className="text-sm text-muted-foreground">Loading...</div>
          ) : stats ? (
            <div className="space-y-1 text-sm text-muted-foreground">
              <div>{stats.code_mappings.total} total mappings</div>
              <div className="text-green-600">{stats.code_mappings.approved} approved</div>
              <div className="text-amber-600">{stats.code_mappings.proposed} proposed</div>
            </div>
          ) : null}
        </button>

        {/* Therapy Mapping Card */}
        <button
          onClick={() => navigate('/therapy-mappings')}
          className="rounded-lg border border-border bg-background p-6 shadow-sm text-left hover:border-primary/50 hover:shadow-md transition-all"
        >
          <div className="flex items-center gap-3 mb-4">
            <div className="rounded-lg bg-emerald-50 p-2.5 text-emerald-600">
              <FlaskConical size={22} />
            </div>
            <h2 className="text-lg font-semibold text-foreground">Therapy Mapping</h2>
          </div>
          {loading ? (
            <div className="text-sm text-muted-foreground">Loading...</div>
          ) : stats ? (
            <div className="space-y-1 text-sm text-muted-foreground">
              <div>{stats.therapy.regimens} regimens</div>
              <div>{stats.therapy.components} components</div>
              <div>{stats.therapy.classes} classes</div>
              <div>{stats.therapy.disease_links} disease links</div>
            </div>
          ) : null}
        </button>
      </div>
    </div>
  );
}
