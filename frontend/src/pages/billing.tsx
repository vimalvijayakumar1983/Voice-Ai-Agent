import { useEffect, useState } from 'react';
import Layout from '@/components/Layout';
import { api } from '@/lib/api';

export default function Billing() {
  const [usage, setUsage] = useState<any>(null);
  const [plans, setPlans] = useState<any[]>([]);

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
          <>
            {[
              { name: 'Starter', price: 49, minutes: 500, agents: 3, concurrent: 5 },
              { name: 'Professional', price: 199, minutes: 2500, agents: 15, concurrent: 25 },
              { name: 'Enterprise', price: 499, minutes: 10000, agents: 100, concurrent: 100 },
            ].map((plan) => (
              <div key={plan.name} className="card" style={{ textAlign: 'center' }}>
                <h3 style={{ marginBottom: 8 }}>{plan.name}</h3>
                <div style={{ fontSize: 36, fontWeight: 700, marginBottom: 4 }}>
                  ${plan.price}<span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>/mo</span>
                </div>
                <div style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 16 }}>
                  {plan.minutes.toLocaleString()} minutes included
                </div>
                <div style={{ textAlign: 'left', marginBottom: 16 }}>
                  {[
                    `${plan.agents} AI Agents`,
                    `${plan.concurrent} concurrent calls`,
                    `${plan.minutes.toLocaleString()} minutes/mo`,
                    'Call transcripts & summaries',
                    'Webhook integrations',
                    plan.price >= 199 ? 'Campaign management' : null,
                    plan.price >= 499 ? 'Custom integrations' : null,
                    plan.price >= 499 ? 'Dedicated support' : null,
                  ].filter(Boolean).map((feature, i) => (
                    <div key={i} style={{ padding: '4px 0', fontSize: 13, color: 'var(--text-secondary)' }}>
                      + {feature}
                    </div>
                  ))}
                </div>
                <button className="btn btn-primary" style={{ width: '100%', justifyContent: 'center' }}>
                  Select Plan
                </button>
              </div>
            ))}
          </>
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
