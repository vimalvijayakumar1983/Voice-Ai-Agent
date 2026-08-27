import { useEffect, useState } from 'react';
import { PhoneOutgoing, Plus, X } from 'lucide-react';
import Layout from '@/components/Layout';
import { api, CallRecord, VoiceAgent } from '@/lib/api';

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
  const [calls, setCalls] = useState<CallRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedCall, setSelectedCall] = useState<CallRecord | null>(null);
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [showDialer, setShowDialer] = useState(false);
  const [dialing, setDialing] = useState(false);
  const [dialForm, setDialForm] = useState({ agent_id: '', to_number: '' });

  useEffect(() => {
    api.listCalls().then(setCalls).catch(() => {}).finally(() => setLoading(false));
    api.listAgents().then(setAgents).catch(() => {});
  }, []);

  const initiateCall = async (event: React.FormEvent) => {
    event.preventDefault();
    setDialing(true);
    try {
      const call = await api.initiateCall(dialForm);
      setCalls((current) => [call, ...current]);
      setDialForm({ agent_id: '', to_number: '' });
      setShowDialer(false);
    } catch (error) {
      alert(error instanceof Error ? error.message : 'Could not start the call.');
    } finally {
      setDialing(false);
    }
  };

  return (
    <Layout>
      <div className="page-header">
        <div><span className="page-kicker">Monitor & improve</span><h1>Conversations</h1><p className="page-subtitle">Place controlled outbound calls and review every transcript, outcome, and recording.</p></div>
        <div style={{ display: 'flex', gap: 8 }}>
          <select className="btn btn-secondary" onChange={(e) => {
            const params: Record<string, string> = e.target.value
              ? { direction: e.target.value }
              : {};
            api.listCalls(params).then(setCalls);
          }}>
            <option value="">All Directions</option>
            <option value="inbound">Inbound</option>
            <option value="outbound">Outbound</option>
          </select>
          <button className="btn btn-primary" onClick={() => setShowDialer((visible) => !visible)}>{showDialer ? <X size={14} /> : <Plus size={14} />}{showDialer ? 'Close' : 'New call'}</button>
        </div>
      </div>

      {showDialer && <form className="card" style={{ marginBottom: 18 }} onSubmit={initiateCall}><div className="card-title"><div><h3>Start a Smallest.ai outbound call</h3><p>Numbers must use E.164 format. The provider conversation ID will be stored automatically.</p></div><PhoneOutgoing size={18} color="var(--accent)" /></div><div className="form-grid"><div className="form-group"><label>Agent</label><select required value={dialForm.agent_id} onChange={(event) => setDialForm({ ...dialForm, agent_id: event.target.value })}><option value="">Select a provisioned agent</option>{agents.filter((agent) => agent.provider_agent_id).map((agent) => <option value={agent.id} key={agent.id}>{agent.name}</option>)}</select></div><div className="form-group"><label>Customer phone</label><input required value={dialForm.to_number} placeholder="+971501234567" onChange={(event) => setDialForm({ ...dialForm, to_number: event.target.value })} /></div></div><button className="btn btn-primary" disabled={dialing}>{dialing ? 'Starting…' : 'Start outbound call'}</button></form>}

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
