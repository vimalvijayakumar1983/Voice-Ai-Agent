import Link from 'next/link';
import { useEffect, useMemo, useState } from 'react';
import {
  ArrowUpRight,
  Bot,
  CheckCircle2,
  CircleAlert,
  Clock3,
  Loader2,
  PhoneCall,
  RefreshCw,
  Sparkles,
  Timer,
  WalletCards,
  Zap,
} from 'lucide-react';
import Layout from '@/components/Layout';
import { AnalyticsOverview, AnalyticsTimeSeries, api, CurrentUser, ProviderStatus, VoiceAgent } from '@/lib/api';

export default function Dashboard() {
  const [overview, setOverview] = useState<AnalyticsOverview | null>(null);
  const [timeseries, setTimeseries] = useState<AnalyticsTimeSeries['data'] | null>(null);
  const [agents, setAgents] = useState<VoiceAgent[] | null>(null);
  const [provider, setProvider] = useState<ProviderStatus | null>(null);
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [days, setDays] = useState(30);
  const [loading, setLoading] = useState(true);
  const [failedSections, setFailedSections] = useState<string[]>([]);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let active = true;
    Promise.allSettled([
      api.getOverview(days),
      api.getTimeseries(days),
      api.listAgents(),
      api.getProviderStatus(),
      api.getMe(),
    ]).then(([overviewResult, timeseriesResult, agentsResult, providerResult, userResult]) => {
      if (!active) return;
      const failures: string[] = [];
      if (overviewResult.status === 'fulfilled') setOverview(overviewResult.value);
      else failures.push('summary metrics');
      if (timeseriesResult.status === 'fulfilled') setTimeseries(timeseriesResult.value.data);
      else failures.push('conversation volume');
      if (agentsResult.status === 'fulfilled') setAgents(agentsResult.value);
      else failures.push('agent status');
      if (providerResult.status === 'fulfilled') setProvider(providerResult.value);
      else failures.push('provider status');
      if (userResult.status === 'fulfilled') setUser(userResult.value);
      else failures.push('workspace profile');
      setFailedSections(failures);
      setLoading(false);
    });
    return () => { active = false; };
  }, [days, reloadKey]);

  const completionRate = useMemo(() => {
    if (!overview?.total_calls) return null;
    return Math.round(((overview.calls_by_status.completed || 0) / overview.total_calls) * 100);
  }, [overview]);

  const chart = useMemo(() => {
    const visible = (timeseries ?? []).slice(-14);
    const maximum = Math.max(...visible.map((point) => point.calls), 1);
    return visible.map((point) => ({
      ...point,
      height: Math.max((point.calls / maximum) * 100, point.calls ? 8 : 2),
    }));
  }, [timeseries]);
  const activeAgents = agents?.filter((agent) => agent.is_active).length ?? null;
  const syncedAgents = agents?.filter((agent) => agent.sync_status === 'synced').length ?? null;
  const prepareReload = () => {
    setLoading(true);
    setFailedSections([]);
    setOverview(null);
    setTimeseries(null);
    setAgents(null);
    setProvider(null);
    setUser(null);
  };
  const retry = () => {
    prepareReload();
    setReloadKey((value) => value + 1);
  };

  return (
    <Layout>
      <div className="page-header">
        <div>
          <span className="page-kicker">Voice operations</span>
          <h1>Welcome back{user?.full_name ? `, ${user.full_name.split(/\s+/)[0]}` : ''}</h1>
          <p className="page-subtitle">Recorded call activity and configuration signals for this workspace.</p>
        </div>
        <div className="header-actions">
          <select
            className="field-control"
            value={days}
            onChange={(event) => {
              prepareReload();
              setDays(Number(event.target.value));
            }}
            aria-label="Reporting period"
          >
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </select>
          <Link href="/agents" className="btn btn-primary"><Sparkles size={14} /> New agent</Link>
        </div>
      </div>

      {loading && (
        <div className="page-loading" role="status" aria-live="polite">
          <Loader2 className="spin" size={16} /> Loading workspace operations…
        </div>
      )}

      {!loading && failedSections.length > 0 && (
        <div className="provider-alert provider-alert-error" role="alert">
          <CircleAlert size={15} />
          <span>Some dashboard data could not be loaded: {failedSections.join(', ')}. Unavailable values are shown as dashes, not zeros.</span>
          <button type="button" className="btn btn-secondary btn-sm" onClick={retry}>
            <RefreshCw size={12} /> Retry
          </button>
        </div>
      )}

      <section className="hero-strip">
        <div>
          <h2>Review deployment signals before the next conversation.</h2>
          <p>Provider credentials, signed callbacks, and a synced agent are separate requirements. These checks show configuration state, not end-to-end call certification.</p>
        </div>
        <div className="hero-provider">
          <span>Primary voice provider</span>
          <strong>{provider ? (provider.configured ? '● Smallest.ai key configured' : '○ Smallest.ai setup required') : '— Status unavailable'}</strong>
        </div>
      </section>

      <div className="stats-grid">
        <MetricCard
          label="Recorded conversations"
          value={overview ? overview.total_calls.toLocaleString() : '—'}
          detail={overview ? (agents ? `${activeAgents} active agent${activeAgents === 1 ? '' : 's'}` : 'Agent status unavailable') : 'Summary unavailable'}
          icon={<PhoneCall size={17} />}
        />
        <MetricCard
          label="Recorded minutes"
          value={overview ? overview.total_minutes.toFixed(1) : '—'}
          detail={overview?.total_calls ? 'Calls in the selected period' : overview ? 'No calls recorded in this period' : 'Summary unavailable'}
          icon={<Timer size={17} />}
        />
        <MetricCard
          label="Provider completion rate"
          value={completionRate === null ? (overview ? '—' : '—') : `${completionRate}%`}
          detail={overview?.total_calls ? 'Calls with completed status' : overview ? 'No denominator yet' : 'Summary unavailable'}
          icon={<CheckCircle2 size={17} />}
        />
        <MetricCard
          label="Average recorded duration"
          value={overview ? `${Math.round(overview.avg_duration_seconds)}s` : '—'}
          detail={overview ? `Recorded cost estimate $${(overview.total_cost_cents / 100).toFixed(2)} · verify provider invoice` : 'Summary unavailable'}
          icon={<WalletCards size={17} />}
        />
      </div>

      <div className="dashboard-grid">
        <section className="card">
          <div className="card-title">
            <div><h3>Conversation volume</h3><p>Daily call activity for the selected period</p></div>
            <span className="badge badge-neutral"><ArrowUpRight size={11} /> Recorded data</span>
          </div>
          <div className="chart-placeholder" aria-label="Conversation volume chart" aria-busy={loading}>
            {timeseries === null
              ? <p className="chart-empty">Conversation volume is unavailable.</p>
              : chart.length === 0
                ? <p className="chart-empty">No conversation activity was recorded in this period.</p>
              : chart.map((point) => (
                <div
                  className="chart-bar"
                  style={{ height: `${point.height}%` }}
                  key={point.date}
                  title={`${point.date}: ${point.calls} call${point.calls === 1 ? '' : 's'}`}
                />
              ))}
          </div>
        </section>

        <section className="card">
          <div className="card-title"><div><h3>Workspace readiness</h3><p>Critical launch checks</p></div><Zap size={16} color="var(--accent)" /></div>
          <div className="activity-list">
            <ReadinessItem
              icon={<Bot size={14} />}
              title="Published agent"
              detail={agents ? `${syncedAgents} of ${agents.length} agent${agents.length === 1 ? '' : 's'} synced` : 'Agent status unavailable'}
              state={agents ? (Boolean(syncedAgents) ? 'configured' : 'action') : 'unknown'}
            />
            <ReadinessItem
              icon={<Sparkles size={14} />}
              title="Smallest.ai API"
              detail={provider ? (provider.configured ? 'Server-side key configured' : 'Server-side key required') : 'Provider status unavailable'}
              state={provider ? (provider.configured ? 'configured' : 'action') : 'unknown'}
            />
            <ReadinessItem
              icon={<PhoneCall size={14} />}
              title="Recorded call evidence"
              detail={overview ? (overview.total_calls ? `${overview.total_calls} call${overview.total_calls === 1 ? '' : 's'} in this period` : 'No completed end-to-end call evidence yet') : 'Call evidence unavailable'}
              state={overview ? (overview.total_calls ? 'configured' : 'action') : 'unknown'}
            />
            <ReadinessItem
              icon={<Clock3 size={14} />}
              title="Signed provider callbacks"
              detail={provider ? (provider.webhook_configured ? 'Signing secret configured' : 'Signing secret required') : 'Webhook status unavailable'}
              state={provider ? (provider.webhook_configured ? 'configured' : 'action') : 'unknown'}
            />
          </div>
          <p className="form-hint">Configuration checks do not prove multilingual switching, tool execution, transfer behavior, or production call quality.</p>
        </section>
      </div>
    </Layout>
  );
}

function MetricCard({ label, value, detail, icon }: { label: string; value: string; detail: string; icon: React.ReactNode }) {
  return <div className="stat-card"><div className="stat-icon">{icon}</div><div className="label">{label}</div><div className="value">{value}</div><div className="stat-meta">{detail}</div></div>;
}

function ReadinessItem({
  icon,
  title,
  detail,
  state,
}: {
  icon: React.ReactNode;
  title: string;
  detail: string;
  state: 'configured' | 'action' | 'unknown';
}) {
  const label = state === 'configured' ? 'Configured' : state === 'action' ? 'Review' : 'Unknown';
  const badge = state === 'configured' ? 'badge-success' : state === 'action' ? 'badge-warning' : 'badge-neutral';
  return <div className="activity-item"><div className="activity-icon">{icon}</div><div><strong>{title}</strong><p>{detail}</p></div><span className={`badge ${badge}`}>{label}</span></div>;
}
