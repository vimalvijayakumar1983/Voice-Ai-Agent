import Link from 'next/link';
import { useEffect, useMemo, useState } from 'react';
import {
  ArrowUpRight,
  Bot,
  CheckCircle2,
  Clock3,
  PhoneCall,
  Sparkles,
  Timer,
  WalletCards,
  Zap,
} from 'lucide-react';
import Layout from '@/components/Layout';
import { AnalyticsOverview, AnalyticsTimeSeries, api, ProviderStatus, VoiceAgent } from '@/lib/api';

const emptyOverview: AnalyticsOverview = {
  total_calls: 0,
  total_minutes: 0,
  avg_duration_seconds: 0,
  total_cost_cents: 0,
  calls_by_status: {},
  calls_by_direction: {},
  calls_by_disposition: {},
};

export default function Dashboard() {
  const [overview, setOverview] = useState<AnalyticsOverview>(emptyOverview);
  const [timeseries, setTimeseries] = useState<AnalyticsTimeSeries['data']>([]);
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [provider, setProvider] = useState<ProviderStatus | null>(null);
  const [days, setDays] = useState(30);

  useEffect(() => {
    Promise.allSettled([
      api.getOverview(days),
      api.getTimeseries(days),
      api.listAgents(),
      api.getProviderStatus(),
    ]).then(([overviewResult, timeseriesResult, agentsResult, providerResult]) => {
      if (overviewResult.status === 'fulfilled') setOverview(overviewResult.value);
      if (timeseriesResult.status === 'fulfilled') setTimeseries(timeseriesResult.value.data);
      if (agentsResult.status === 'fulfilled') setAgents(agentsResult.value);
      if (providerResult.status === 'fulfilled') setProvider(providerResult.value);
    });
  }, [days]);

  const completionRate = useMemo(() => {
    if (!overview.total_calls) return 0;
    return Math.round(((overview.calls_by_status.completed || 0) / overview.total_calls) * 100);
  }, [overview]);

  const chart = useMemo(() => {
    const visible = timeseries.slice(-14);
    const maximum = Math.max(...visible.map((point) => point.calls), 1);
    return visible.map((point) => ({
      ...point,
      height: Math.max((point.calls / maximum) * 100, point.calls ? 8 : 2),
    }));
  }, [timeseries]);
  const activeAgents = agents.filter((agent) => agent.is_active).length;

  return (
    <Layout>
      <div className="page-header">
        <div>
          <span className="page-kicker">Voice operations</span>
          <h1>Welcome back, Vimal</h1>
          <p className="page-subtitle">A clear view of every AI conversation, agent, and customer outcome across Al Zaabi Group.</p>
        </div>
        <div className="header-actions">
          <select className="field-control" value={days} onChange={(event) => setDays(Number(event.target.value))} aria-label="Reporting period">
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </select>
          <Link href="/agents" className="btn btn-primary"><Sparkles size={14} /> New agent</Link>
        </div>
      </div>

      <section className="hero-strip">
        <div>
          <h2>Your real-time voice stack is ready for the next conversation.</h2>
          <p>Build, test, deploy, and improve multilingual agents from one governed workspace—with every call captured for QA and commercial insight.</p>
        </div>
        <div className="hero-provider">
          <span>Primary voice provider</span>
          <strong>{provider?.configured ? '● Smallest.ai connected' : '○ Smallest.ai setup required'}</strong>
        </div>
      </section>

      <div className="stats-grid">
        <MetricCard label="Total conversations" value={overview.total_calls.toLocaleString()} detail={`${activeAgents} active agent${activeAgents === 1 ? '' : 's'}`} icon={<PhoneCall size={17} />} />
        <MetricCard label="Conversation minutes" value={overview.total_minutes.toFixed(1)} detail="Across voice and web calls" icon={<Timer size={17} />} />
        <MetricCard label="Completion rate" value={`${completionRate}%`} detail="Answered and completed" icon={<CheckCircle2 size={17} />} />
        <MetricCard label="Average handling time" value={`${Math.round(overview.avg_duration_seconds)}s`} detail={`Estimated cost $${(overview.total_cost_cents / 100).toFixed(2)}`} icon={<WalletCards size={17} />} />
      </div>

      <div className="dashboard-grid">
        <section className="card">
          <div className="card-title">
            <div><h3>Conversation volume</h3><p>Daily call activity for the selected period</p></div>
            <span className="badge badge-success"><ArrowUpRight size={11} /> Live reporting</span>
          </div>
          <div className="chart-placeholder" aria-label="Conversation volume chart">
            {chart.length === 0
              ? <p className="chart-empty">No conversation activity in this period.</p>
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
            <ReadinessItem icon={<Bot size={14} />} title="Agent workspace" detail={`${agents.length} configured`} ready={agents.length > 0} />
            <ReadinessItem icon={<Sparkles size={14} />} title="Smallest.ai API" detail={provider?.configured ? 'Secure server key found' : 'Add server API key'} ready={Boolean(provider?.configured)} />
            <ReadinessItem icon={<PhoneCall size={14} />} title="Call intelligence" detail="Transcripts and outcomes" ready />
            <ReadinessItem icon={<Clock3 size={14} />} title="Webhook lifecycle" detail={provider?.webhook_configured ? 'Signature verification active' : 'Signing secret required'} ready={Boolean(provider?.webhook_configured)} />
          </div>
        </section>
      </div>
    </Layout>
  );
}

function MetricCard({ label, value, detail, icon }: { label: string; value: string; detail: string; icon: React.ReactNode }) {
  return <div className="stat-card"><div className="stat-icon">{icon}</div><div className="label">{label}</div><div className="value">{value}</div><div className="stat-meta">{detail}</div></div>;
}

function ReadinessItem({ icon, title, detail, ready }: { icon: React.ReactNode; title: string; detail: string; ready: boolean }) {
  return <div className="activity-item"><div className="activity-icon">{icon}</div><div><strong>{title}</strong><p>{detail}</p></div><span className={`badge ${ready ? 'badge-success' : 'badge-warning'}`}>{ready ? 'Ready' : 'Action'}</span></div>;
}
