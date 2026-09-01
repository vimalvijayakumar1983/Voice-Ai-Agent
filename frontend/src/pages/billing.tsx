import { useEffect, useMemo, useState } from 'react';
import {
  Activity,
  BarChart3,
  CheckCircle2,
  CircleAlert,
  CircleDollarSign,
  Clock3,
  Download,
  ExternalLink,
  Filter,
  Loader2,
  PhoneCall,
  RefreshCw,
  TrendingUp,
  WalletCards,
} from 'lucide-react';
import Layout from '@/components/Layout';
import { api, CostReport, CostReportFilters, VoiceAgent } from '@/lib/api';

const STATUS_LABELS: Record<string, string> = {
  completed: 'Completed',
  failed: 'Failed',
  no_answer: 'No answer',
  busy: 'Busy',
  in_progress: 'In progress',
  initiated: 'Initiated',
  ringing: 'Ringing',
  dispatching: 'Dispatching',
  terminal_unknown: 'Needs review',
};

function messageFrom(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date(value));
}

function formatDateTime(value: string) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value));
}

function formatDuration(seconds: number) {
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function money(value: number, currency: 'USD' | 'AED') {
  const digits = Math.abs(value) < 0.01 && value !== 0 ? 4 : 2;
  return new Intl.NumberFormat('en-AE', {
    style: 'currency',
    currency,
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}

function MoneyPair({ usd, aed, compact = false }: { usd: number; aed: number; compact?: boolean }) {
  return <span className={`cost-money-pair${compact ? ' compact' : ''}`}><strong>{money(usd, 'USD')}</strong><span>{money(aed, 'AED')}</span></span>;
}

function percentage(value: number) {
  return `${Math.round(value * 100)}%`;
}

function providerName(value: string | null) {
  if (!value) return '—';
  if (value === 'smallest') return 'Smallest.ai';
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function costStateLabel(value: string) {
  if (value === 'public_rate_estimate') return 'Public-rate estimate';
  if (value === 'recorded_ledger_estimate') return 'Ledger estimate';
  if (value === 'zero_duration') return 'Zero duration';
  return 'Unpriced';
}

export default function Billing() {
  const [report, setReport] = useState<CostReport | null>(null);
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [filters, setFilters] = useState<CostReportFilters>({ days: 30 });
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState('');
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let active = true;
    api.getCostReport(filters)
      .then((value) => { if (active) setReport(value); })
      .catch((loadError) => {
        if (!active) return;
        setReport(null);
        setError(messageFrom(loadError, 'The cost and call report could not be loaded.'));
      })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [filters, reloadKey]);
  useEffect(() => { api.listAgents().then(setAgents).catch(() => setAgents([])); }, []);

  const updateFilter = (name: keyof CostReportFilters, value: string | number) => {
    setLoading(true);
    setError('');
    setFilters((current) => ({ ...current, [name]: value }));
  };
  const clearFilters = () => {
    setLoading(true);
    setError('');
    setFilters({ days: filters.days || 30 });
  };
  const refresh = () => {
    setLoading(true);
    setError('');
    setReloadKey((value) => value + 1);
  };

  const exportCsv = async () => {
    setExporting(true);
    setError('');
    try {
      const blob = await api.downloadCostReport(filters);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `vav-cost-call-report-${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (exportError) {
      setError(messageFrom(exportError, 'The CSV report could not be exported.'));
    } finally {
      setExporting(false);
    }
  };

  const trend = report?.trend.slice(-31) || [];
  const maxTrendCost = Math.max(...trend.map((point) => point.cost_aed), 0.000001);
  const activeFilterCount = Object.entries(filters).filter(([key, value]) => key !== 'days' && Boolean(value)).length;
  const providerGroups = useMemo(() => {
    const groups = new Map<string, { usd: number; aed: number; services: number }>();
    for (const row of report?.provider_breakdown || []) {
      const current = groups.get(row.provider) || { usd: 0, aed: 0, services: 0 };
      current.usd += row.cost_usd;
      current.aed += row.cost_aed;
      current.services += 1;
      groups.set(row.provider, current);
    }
    return Array.from(groups.entries()).sort((left, right) => right[1].usd - left[1].usd);
  }, [report]);

  return (
    <Layout>
      <div className="page-header cost-report-header">
        <div><span className="page-kicker">Finance & operations intelligence</span><h1>Provider cost & call reports</h1><p className="page-subtitle">Traceable USD and AED cost estimates, call performance, provider comparisons, and export-ready detail.</p></div>
        <div className="cost-header-actions">
          <button className="btn btn-secondary" type="button" onClick={refresh} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh</button>
          <button className="btn btn-primary" type="button" onClick={exportCsv} disabled={loading || exporting || !report}>{exporting ? <Loader2 className="spin" size={14} /> : <Download size={14} />} Export CSV</button>
        </div>
      </div>

      <section className="cost-filter-bar" aria-label="Report filters">
        <div className="cost-filter-heading"><Filter size={15} /><strong>Filters</strong>{activeFilterCount > 0 && <span className="badge badge-neutral">{activeFilterCount} active</span>}</div>
        <label><span>Period</span><select value={filters.days || 30} onChange={(event) => updateFilter('days', Number(event.target.value))}><option value={7}>Last 7 days</option><option value={30}>Last 30 days</option><option value={90}>Last 90 days</option><option value={365}>Last 12 months</option></select></label>
        <label><span>Telephony</span><select value={filters.provider || ''} onChange={(event) => updateFilter('provider', event.target.value)}><option value="">All providers</option><option value="twilio">Twilio</option><option value="smallest">Smallest.ai</option></select></label>
        <label><span>Speech</span><select value={filters.speech_provider || ''} onChange={(event) => updateFilter('speech_provider', event.target.value)}><option value="">All speech</option><option value="sarvam">Sarvam</option><option value="elevenlabs">ElevenLabs</option><option value="smallest">Smallest.ai</option></select></label>
        <label><span>Agent</span><select value={filters.agent_id || ''} onChange={(event) => updateFilter('agent_id', event.target.value)}><option value="">All agents</option>{agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></label>
        <label><span>Direction</span><select value={filters.direction || ''} onChange={(event) => updateFilter('direction', event.target.value)}><option value="">Inbound & outbound</option><option value="inbound">Inbound</option><option value="outbound">Outbound</option></select></label>
        {activeFilterCount > 0 && <button className="btn btn-secondary btn-sm" type="button" onClick={clearFilters}>Clear</button>}
      </section>

      {loading && !report && <div className="page-loading" role="status"><Loader2 className="spin" size={16} /> Building the cost and call report…</div>}
      {error && <div className="provider-alert provider-alert-error" role="alert"><CircleAlert size={15} /><span>{error}</span><button className="btn btn-secondary btn-sm" type="button" onClick={refresh}>Retry</button></div>}

      {report && <>
        <div className="cost-report-period"><div><Activity size={14} /><span>{formatDate(report.period_start)}–{formatDate(report.period_end)}</span></div><div><span>FX snapshot</span><strong>1 USD = {report.currency.usd_to_aed.toFixed(4)} AED</strong><a href={report.currency.source_url} target="_blank" rel="noreferrer">CBUAE <ExternalLink size={11} /></a></div></div>

        <div className="cost-kpi-grid">
          <article className="cost-kpi cost-kpi-primary"><div className="cost-kpi-icon"><WalletCards size={18} /></div><span>Estimated provider cost</span><MoneyPair usd={report.summary.estimated_cost_usd} aed={report.summary.estimated_cost_aed} /><small>Public-rate components; not an invoice</small></article>
          <article className="cost-kpi"><div className="cost-kpi-icon"><CircleDollarSign size={18} /></div><span>Average cost / call</span><MoneyPair usd={report.summary.avg_cost_per_call_usd} aed={report.summary.avg_cost_per_call_aed} /><small>{report.summary.total_calls.toLocaleString()} calls in scope</small></article>
          <article className="cost-kpi"><div className="cost-kpi-icon"><Clock3 size={18} /></div><span>Cost / connected minute</span><MoneyPair usd={report.summary.cost_per_minute_usd} aed={report.summary.cost_per_minute_aed} /><small>{report.summary.total_minutes.toFixed(1)} connected minutes</small></article>
          <article className="cost-kpi"><div className="cost-kpi-icon"><CheckCircle2 size={18} /></div><span>Cost coverage</span><strong className="cost-kpi-number">{percentage(report.summary.cost_coverage)}</strong><small>{report.summary.fully_priced_calls} fully priced · {report.summary.unpriced_calls} unpriced</small></article>
          <article className="cost-kpi"><div className="cost-kpi-icon"><PhoneCall size={18} /></div><span>Answer rate</span><strong className="cost-kpi-number">{percentage(report.summary.answer_rate)}</strong><small>{report.summary.answered_calls} of {report.summary.total_calls} calls answered</small></article>
          <article className="cost-kpi"><div className="cost-kpi-icon"><TrendingUp size={18} /></div><span>Successful outcome rate</span><strong className="cost-kpi-number">{percentage(report.summary.success_rate)}</strong><small>{report.summary.successful_calls} successful of {report.summary.completed_calls} completed</small></article>
        </div>

        {report.summary.full_cost_coverage < 1 && report.summary.total_calls > 0 && <div className="provider-alert cost-coverage-alert" role="note"><CircleAlert size={15} /><span><strong>{percentage(report.summary.full_cost_coverage)} has complete line-item pricing.</strong> Partial calls remain visible and identify the exact missing input—such as a destination-specific Twilio rate or legacy OpenAI token split.</span></div>}

        <div className="cost-report-grid">
          <section className="card cost-trend-card" aria-labelledby="cost-trend-title"><div className="card-title"><div><h2 id="cost-trend-title">Cost trend</h2><p>Estimated AED cost and call volume by day</p></div><BarChart3 size={18} /></div>{trend.length ? <div className="cost-trend-chart" role="img" aria-label="Daily estimated cost trend">{trend.map((point) => <div key={point.date} className="cost-trend-column" title={`${formatDate(point.date)} · ${money(point.cost_aed, 'AED')} · ${point.calls} calls`}><span className="cost-trend-value">{point.calls}</span><div style={{ height: `${Math.max((point.cost_aed / maxTrendCost) * 100, 3)}%` }} /><time dateTime={point.date}>{new Date(`${point.date}T00:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}</time></div>)}</div> : <div className="empty-state compact"><h3>No trend data</h3><p>No calls were found for this filter.</p></div>}</section>

          <section className="card cost-provider-summary" aria-labelledby="provider-summary-title"><div className="card-title"><div><h2 id="provider-summary-title">Provider share</h2><p>Estimated cost by service provider</p></div></div>{providerGroups.length ? providerGroups.map(([name, values]) => { const share = report.summary.estimated_cost_usd ? values.usd / report.summary.estimated_cost_usd : 0; return <div className="cost-provider-share" key={name}><div><strong>{name}</strong><MoneyPair usd={values.usd} aed={values.aed} compact /></div><div className="cost-share-track"><span style={{ width: `${Math.max(share * 100, 2)}%` }} /></div><small>{percentage(share)} · {values.services} service{values.services === 1 ? '' : 's'}</small></div>; }) : <div className="empty-state compact"><h3>No priced providers</h3><p>Usage is currently unpriced for this filter.</p></div>}</section>
        </div>

        <section className="card cost-breakdown-card" aria-labelledby="breakdown-title"><div className="card-title"><div><h2 id="breakdown-title">Provider cost breakdown</h2><p>Every public-rate component is tied to its source; unallocated legacy estimates are labeled separately.</p></div><span className="badge badge-neutral">USD + AED</span></div>{report.provider_breakdown.length ? <div className="table-container"><table className="cost-breakdown-table"><thead><tr><th>Provider / service</th><th>Calls</th><th>Measured usage</th><th>Basis</th><th>Estimated cost</th></tr></thead><tbody>{report.provider_breakdown.map((row) => <tr key={`${row.provider}-${row.service}`}><td><strong>{row.provider}</strong><span>{row.service} {row.source_url && <a href={row.source_url} target="_blank" rel="noreferrer" aria-label={`Open ${row.provider} pricing source`}><ExternalLink size={11} /></a>}</span></td><td>{row.calls.toLocaleString()}</td><td>{row.quantity.toLocaleString(undefined, { maximumFractionDigits: 3 })} <span>{row.unit}</span></td><td><span className="cost-basis">{row.basis}</span></td><td><MoneyPair usd={row.cost_usd} aed={row.cost_aed} compact /></td></tr>)}</tbody></table></div> : <div className="empty-state compact"><h3>No measurable provider costs</h3><p>Calls will remain unpriced rather than receiving a fabricated provider total.</p></div>}</section>

        <section className="card cost-call-card" aria-labelledby="call-detail-title"><div className="card-title"><div><h2 id="call-detail-title">Call-level cost report</h2><p>Operational result, duration, provider path, and costing state for each call.</p></div><span className="badge badge-neutral">{report.calls.length} rows</span></div>{report.calls.length ? <div className="table-container"><table className="cost-call-table"><thead><tr><th>Call</th><th>Agent</th><th>Provider path</th><th>Result</th><th>Duration</th><th>Estimated cost</th><th>Coverage</th></tr></thead><tbody>{report.calls.map((call) => <tr key={call.call_id}><td><strong>{formatDateTime(call.created_at)}</strong><span>{call.direction === 'inbound' ? call.from_number : call.to_number}</span></td><td>{call.agent_name}</td><td><strong>{providerName(call.telephony_provider)}</strong><span>{providerName(call.speech_provider)} speech</span></td><td><span className={`badge ${call.status === 'completed' ? 'badge-success' : call.status === 'failed' ? 'badge-danger' : 'badge-neutral'}`}>{STATUS_LABELS[call.status] || call.status.replace(/_/g, ' ')}</span><span>{call.disposition?.replace(/_/g, ' ') || 'No disposition'}</span></td><td>{formatDuration(call.duration_seconds)}</td><td><MoneyPair usd={call.cost_usd} aed={call.cost_aed} compact /></td><td><span className={`cost-state ${call.cost_state === 'unpriced' ? 'unpriced' : ''}`}>{costStateLabel(call.cost_state)}</span>{call.missing_cost_inputs.length > 0 && <span className="cost-missing" title={call.missing_cost_inputs.join(', ')}>{call.missing_cost_inputs.length} input{call.missing_cost_inputs.length === 1 ? '' : 's'} missing</span>}</td></tr>)}</tbody></table></div> : <div className="empty-state"><h3>No calls in this period</h3><p>Adjust the filters or place a call to begin building the report.</p></div>}</section>

        <section className="card cost-rate-card" aria-labelledby="rate-reference-title"><div className="card-title"><div><h2 id="rate-reference-title">Provider price reference</h2><p>Official public list rates converted side by side for budgeting and comparison.</p></div><span className="badge badge-neutral">Verified references</span></div><div className="table-container"><table><thead><tr><th>Provider</th><th>Service</th><th>Published rate</th><th>USD equivalent</th><th>AED equivalent</th><th>Commercial note</th></tr></thead><tbody>{report.rate_cards.map((rate) => <tr key={`${rate.provider}-${rate.service}`}><td><strong>{rate.provider}</strong></td><td>{rate.service}</td><td>{rate.native_currency === 'INR' ? '₹' : '$'}{rate.native_amount.toLocaleString()} / {rate.unit}</td><td>{money(rate.usd, 'USD')}</td><td>{money(rate.aed, 'AED')}</td><td><span className="cost-rate-note">{rate.notes}</span><a href={rate.source_url} target="_blank" rel="noreferrer">Official source <ExternalLink size={11} /></a></td></tr>)}</tbody></table></div></section>

        <section className="cost-methodology" aria-labelledby="method-title"><div><CircleAlert size={17} /></div><div><h2 id="method-title">Pricing methodology and controls</h2><p>{report.methodology.primary_total}</p><p>{report.methodology.not_included}</p><strong>{report.methodology.invoice_status}</strong><small>{report.currency.notes} FX reference date: {report.currency.fx_effective_date}.</small></div></section>
      </>}
    </Layout>
  );
}
