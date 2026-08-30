import { useEffect, useState } from 'react';
import { CircleAlert, Clock3, Coins, Loader2, RefreshCw, Timer, WalletCards } from 'lucide-react';
import Layout from '@/components/Layout';
import { api, BillingPlan, UsageSummary } from '@/lib/api';

function messageFrom(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date(value));
}

function formatDollars(cents: number) {
  return `$${(cents / 100).toFixed(2)}`;
}

export default function Billing() {
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [plans, setPlans] = useState<BillingPlan[] | null>(null);
  const [usageError, setUsageError] = useState('');
  const [plansError, setPlansError] = useState('');
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let active = true;
    Promise.allSettled([api.getUsage(30), api.getPlans()])
      .then(([usageResult, plansResult]) => {
        if (!active) return;
        if (usageResult.status === 'fulfilled') setUsage(usageResult.value);
        else setUsageError(messageFrom(usageResult.reason, 'Usage could not be loaded.'));
        if (plansResult.status === 'fulfilled') setPlans(plansResult.value);
        else setPlansError(messageFrom(plansResult.reason, 'Plan catalog could not be loaded.'));
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => { active = false; };
  }, [reloadKey]);

  const retry = () => {
    setLoading(true);
    setUsage(null);
    setPlans(null);
    setUsageError('');
    setPlansError('');
    setReloadKey((value) => value + 1);
  };

  return (
    <Layout>
      <div className="page-header">
        <div>
          <span className="page-kicker">Usage reporting</span>
          <h1>Usage & billing</h1>
          <p className="page-subtitle">Review recorded usage and the read-only commercial plan catalog.</p>
        </div>
        <button className="btn btn-secondary" type="button" onClick={retry} disabled={loading}>
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      {loading && (
        <div className="page-loading" role="status" aria-live="polite">
          <Loader2 className="spin" size={16} /> Loading usage and plan data…
        </div>
      )}

      {!loading && (usageError || plansError) && (
        <div className="provider-alert provider-alert-error" role="alert">
          <CircleAlert size={15} />
          <span>
            {usageError && `Usage: ${usageError} `}
            {plansError && `Plans: ${plansError} `}
            Unavailable sections are not shown as zero usage or an empty catalog.
          </span>
          <button className="btn btn-secondary btn-sm" type="button" onClick={retry}>
            <RefreshCw size={12} /> Retry
          </button>
        </div>
      )}

      {usage && (
        <>
          <section className="card-title" aria-labelledby="usage-summary-title">
            <div>
              <h2 id="usage-summary-title">Recorded usage</h2>
              <p>{formatDate(usage.period_start)}–{formatDate(usage.period_end)}</p>
            </div>
            <span className="badge badge-neutral">Reporting only</span>
          </section>
          <div className="stats-grid">
            <div className="stat-card">
              <div className="stat-icon"><Timer size={17} /></div>
              <div className="label">Recorded minutes</div>
              <div className="value">{usage.total_minutes.toFixed(1)}</div>
              <div className="stat-meta">{usage.total_minutes ? 'Usage records in this period' : 'No minutes recorded in this period'}</div>
            </div>
            <div className="stat-card">
              <div className="stat-icon"><Clock3 size={17} /></div>
              <div className="label">Included / overage</div>
              <div className="value">{usage.included_minutes.toLocaleString()} / {usage.overage_minutes.toFixed(1)}</div>
              <div className="stat-meta">Reported allowance and overage minutes</div>
            </div>
            <div className="stat-card">
              <div className="stat-icon"><Coins size={17} /></div>
              <div className="label">Recorded AI tokens</div>
              <div className="value">{usage.total_ai_tokens.toLocaleString()}</div>
              <div className="stat-meta">Token usage reported by this application</div>
            </div>
            <div className="stat-card">
              <div className="stat-icon"><WalletCards size={17} /></div>
              <div className="label">Application cost estimate</div>
              <div className="value">{formatDollars(usage.total_cost_cents)}</div>
              <div className="stat-meta">Not an invoice · verify provider charges and tax</div>
            </div>
          </div>
          <div className="provider-alert" role="note">
            <CircleAlert size={15} />
            <span>Costs are application estimates from recorded usage. Provider invoices, credits, taxes, and reconciliation are not available on this screen.</span>
          </div>
        </>
      )}

      {!loading && !usage && !usageError && (
        <div className="empty-state">
          <h3>No usage summary returned</h3>
          <p>The billing service responded without a report for this period.</p>
        </div>
      )}

      <section className="card" aria-labelledby="plans-title">
        <div className="card-title">
          <div>
            <h2 id="plans-title">Plan reference</h2>
            <p>Read-only catalog; plan selection, checkout, invoices, and payment management are not available in this app.</p>
          </div>
          <span className="badge badge-neutral">No checkout</span>
        </div>

        {plansError ? (
          <div className="empty-state">
            <h3>Plan catalog unavailable</h3>
            <p>Retry to distinguish a service failure from an intentionally empty catalog.</p>
          </div>
        ) : plans === null ? (
          <div className="page-loading" role="status"><Loader2 className="spin" size={16} /> Loading plans…</div>
        ) : plans.length === 0 ? (
          <div className="empty-state">
            <h3>No approved plans configured</h3>
            <p>The billing API returned an empty plan catalog. Contact a workspace owner for commercial terms.</p>
          </div>
        ) : (
          <div className="agent-grid">
            {plans.map((plan) => (
              <article key={plan.id} className="agent-card">
                <div className="agent-card-title"><h3>{plan.name}</h3><p>Reference plan</p></div>
                <p className="agent-card-body"><strong>{formatDollars(plan.base_price_cents)} / month</strong></p>
                <div className="agent-card-meta">
                  <span className="meta-chip">{plan.included_minutes.toLocaleString()} included minutes</span>
                  <span className="meta-chip">{formatDollars(plan.per_minute_cents)} per overage minute</span>
                  <span className="meta-chip">{plan.max_agents} agent limit</span>
                  <span className="meta-chip">{plan.max_concurrent_calls} concurrent-call limit</span>
                </div>
              </article>
            ))}
          </div>
        )}
        <p className="form-hint">Amounts are displayed with “$” because the current API returns cents without a currency code. Confirm currency, tax, and entitlement enforcement before quoting a customer.</p>
      </section>
    </Layout>
  );
}
