import { useEffect, useState } from 'react';
import Layout from '@/components/Layout';
import { api } from '@/lib/api';

function statusBadge(status: string) {
  const map: Record<string, string> = {
    completed: 'badge-success',
    in_progress: 'badge-info',
    ringing: 'badge-warning',
    failed: 'badge-danger',
    no_answer: 'badge-warning',
    busy: 'badge-warning',
  };
  return map[status] || 'badge-info';
}

export default function Calls() {
  const [calls, setCalls] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedCall, setSelectedCall] = useState<any>(null);

  useEffect(() => {
    api.listCalls().then(setCalls).catch(() => {}).finally(() => setLoading(false));
  }, []);

  return (
    <Layout>
      <div className="page-header">
        <h1>Call Logs</h1>
        <div style={{ display: 'flex', gap: 8 }}>
          <select className="btn btn-secondary" onChange={(e) => {
            const params = e.target.value ? { direction: e.target.value } : {};
            api.listCalls(params).then(setCalls);
          }}>
            <option value="">All Directions</option>
            <option value="inbound">Inbound</option>
            <option value="outbound">Outbound</option>
          </select>
        </div>
      </div>

      {loading ? (
        <p style={{ color: 'var(--text-secondary)' }}>Loading calls...</p>
      ) : calls.length === 0 ? (
        <div className="empty-state">
          <h3>No calls yet</h3>
          <p>Calls will appear here once agents start making or receiving calls.</p>
        </div>
      ) : (
        <div className="table-container">
          <table>
            <thead>
              <tr>
                <th>Direction</th>
                <th>From</th>
                <th>To</th>
                <th>Status</th>
                <th>Duration</th>
                <th>Disposition</th>
                <th>Date</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {calls.map((call) => (
                <tr key={call.id}>
                  <td>
                    <span className={`badge ${call.direction === 'inbound' ? 'badge-info' : 'badge-warning'}`}>
                      {call.direction}
                    </span>
                  </td>
                  <td style={{ fontFamily: 'monospace', fontSize: 13 }}>{call.from_number}</td>
                  <td style={{ fontFamily: 'monospace', fontSize: 13 }}>{call.to_number}</td>
                  <td><span className={`badge ${statusBadge(call.status)}`}>{call.status}</span></td>
                  <td>{call.duration_seconds ? `${call.duration_seconds}s` : '-'}</td>
                  <td>{call.disposition || '-'}</td>
                  <td style={{ color: 'var(--text-secondary)', fontSize: 13 }}>
                    {new Date(call.created_at).toLocaleString()}
                  </td>
                  <td>
                    <button className="btn btn-secondary" style={{ fontSize: 12, padding: '4px 10px' }}
                      onClick={() => setSelectedCall(call)}>
                      Details
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selectedCall && (
        <div style={{
          position: 'fixed', top: 0, right: 0, width: 480, height: '100vh',
          background: 'var(--bg-secondary)', borderLeft: '1px solid var(--border)',
          padding: 24, overflowY: 'auto', zIndex: 100,
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 20 }}>
            <h2>Call Details</h2>
            <button className="btn btn-secondary" onClick={() => setSelectedCall(null)}>Close</button>
          </div>
          <div style={{ display: 'grid', gap: 12 }}>
            {Object.entries(selectedCall).filter(([k]) => k !== 'metadata').map(([key, value]) => (
              <div key={key} style={{ display: 'flex', justifyContent: 'space-between', borderBottom: '1px solid var(--border)', padding: '6px 0' }}>
                <span style={{ color: 'var(--text-secondary)', fontSize: 13 }}>{key}</span>
                <span style={{ fontSize: 13 }}>{String(value ?? '-')}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </Layout>
  );
}
