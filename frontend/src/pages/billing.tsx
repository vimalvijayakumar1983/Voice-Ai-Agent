import { useEffect, useState } from 'react';
import Layout from '@/components/Layout';
import { api, BillingPlan, UsageSummary } from '@/lib/api';

export default function Billing() {
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [plans, setPlans] = useState<BillingPlan[]>([]);

  useEffect(() => {
    api.getUsage(30).then(setUsage).catch(() => {});
    api.getPlans().then(setPlans).catch(() => {});
  }, []);

  return (
    <Layout>
      <div className="page-header">
        <h1>Billing & Usage</h1>
      </div>

      {usage && (
        <div className="stats-grid">
          <div className="stat-card">
            <div className="label">Total Minutes</div>
            <div className="value">{usage.total_minutes.toFixed(1)}</div>
          </div>
          <div className="stat-card">
            <div className="label">Included Minutes</div>
            <div className="value">{usage.included_minutes}</div>
          </div>
          <div className="stat-card">
            <div className="label">Overage Minutes</div>
            <div className="value" style={{ color: usage.overage_minutes > 0 ? 'var(--warning)' : 'inherit' }}>
              {usage.overage_minutes.toFixed(1)}
            </div>
          </div>
          <div className="stat-card">
            <div className="label">Current Cost</div>
            <div className="value">${(usage.total_cost_cents / 100).toFixed(2)}</div>
          </div>
        </div>
      )}

      <h2 style={{ marginBottom: 16 }}>Plans</h2>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))', gap: 16 }}>
        {plans.length === 0 ? (
          <div className="empty-state"><h3>No billing plans configured</h3><p>Add approved commercial plans in the platform database before enabling subscription selection.</p></div>
        ) : (
          plans.map((plan) => (
            <div key={plan.id} className="card">
              <h3>{plan.name}</h3>
              <div style={{ fontSize: 28, fontWeight: 700 }}>${(plan.base_price_cents / 100).toFixed(0)}/mo</div>
              <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
                {plan.included_minutes} minutes, {plan.max_agents} agents
              </p>
            </div>
          ))
        )}
      </div>
    </Layout>
  );
}
