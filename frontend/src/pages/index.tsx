import { useEffect, useState } from 'react';
import Layout from '@/components/Layout';
import { api } from '@/lib/api';

export default function Dashboard() {
  const [overview, setOverview] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.getOverview(30)
      .then(setOverview)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  return (
    <Layout>
      <div className="page-header">
        <h1>Dashboard</h1>
        <select
          style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', color: 'var(--text-primary)', padding: '6px 12px', borderRadius: 6 }}
          onChange={(e) => {
            setLoading(true);
            api.getOverview(parseInt(e.target.value)).then(setOverview).finally(() => setLoading(false));
          }}
        >
          <option value="7">Last 7 days</option>
          <option value="30" selected>Last 30 days</option>
          <option value="90">Last 90 days</option>
        </select>
      </div>

      {loading ? (
        <p style={{ color: 'var(--text-secondary)' }}>Loading analytics...</p>
      ) : overview ? (
        <>
          <div className="stats-grid">
            <div className="stat-card">
              <div className="label">Total Calls</div>
              <div className="value">{overview.total_calls.toLocaleString()}</div>
            </div>
            <div className="stat-card">
              <div className="label">Total Minutes</div>
              <div className="value">{overview.total_minutes.toFixed(1)}</div>
            </div>
            <div className="stat-card">
              <div className="label">Avg Duration</div>
              <div className="value">{Math.round(overview.avg_duration_seconds)}s</div>
            </div>
            <div className="stat-card">
              <div className="label">Total Cost</div>
              <div className="value">${(overview.total_cost_cents / 100).toFixed(2)}</div>
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            <div className="card">
              <h3 style={{ marginBottom: 12 }}>Calls by Status</h3>
              {Object.entries(overview.calls_by_status || {}).map(([status, count]) => (
                <div key={status} style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid var(--border)' }}>
                  <span>{status}</span>
                  <span style={{ fontWeight: 600 }}>{String(count)}</span>
                </div>
              ))}
              {Object.keys(overview.calls_by_status || {}).length === 0 && (
                <p style={{ color: 'var(--text-secondary)', fontSize: 14 }}>No call data yet</p>
              )}
            </div>
            <div className="card">
              <h3 style={{ marginBottom: 12 }}>Calls by Disposition</h3>
              {Object.entries(overview.calls_by_disposition || {}).map(([disp, count]) => (
                <div key={disp} style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid var(--border)' }}>
                  <span>{disp}</span>
                  <span style={{ fontWeight: 600 }}>{String(count)}</span>
                </div>
              ))}
              {Object.keys(overview.calls_by_disposition || {}).length === 0 && (
                <p style={{ color: 'var(--text-secondary)', fontSize: 14 }}>No disposition data yet</p>
              )}
            </div>
          </div>
        </>
      ) : (
        <div className="empty-state">
          <h3>Welcome to Voice AI Agent</h3>
          <p>Create your first agent to get started.</p>
        </div>
      )}
    </Layout>
  );
}
