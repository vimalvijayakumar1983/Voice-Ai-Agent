import { useEffect, useState } from 'react';
import Layout from '@/components/Layout';
import { api, Campaign, VoiceAgent } from '@/lib/api';

export default function Campaigns() {
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({
    name: '', agent_id: '', max_concurrent_calls: 5, calling_hours_start: '09:00', calling_hours_end: '17:00',
  });

  const load = () => {
    api.listCampaigns().then(setCampaigns).catch(() => {});
    api.listAgents().then(setAgents).catch(() => {});
  };

  useEffect(() => { load(); }, []);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.createCampaign(form);
      setShowCreate(false);
      load();
    } catch (error: unknown) {
      alert(error instanceof Error ? error.message : 'Could not create the campaign.');
    }
  };

  const statusColor: Record<string, string> = {
    draft: 'badge-info', running: 'badge-success', paused: 'badge-warning',
    completed: 'badge-success', cancelled: 'badge-danger',
  };

  return (
    <Layout>
      <div className="page-header">
        <h1>Campaigns</h1>
        <button className="btn btn-primary" onClick={() => setShowCreate(!showCreate)}>
          {showCreate ? 'Cancel' : '+ New Campaign'}
        </button>
      </div>

      {showCreate && (
        <div className="card" style={{ marginBottom: 24 }}>
          <form onSubmit={handleCreate}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
              <div className="form-group">
                <label>Campaign Name</label>
                <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
              </div>
              <div className="form-group">
                <label>Agent</label>
                <select value={form.agent_id} onChange={(e) => setForm({ ...form, agent_id: e.target.value })} required>
                  <option value="">Select an agent</option>
                  {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                </select>
              </div>
              <div className="form-group">
                <label>Calling Hours Start</label>
                <input type="time" value={form.calling_hours_start} onChange={(e) => setForm({ ...form, calling_hours_start: e.target.value })} />
              </div>
              <div className="form-group">
                <label>Calling Hours End</label>
                <input type="time" value={form.calling_hours_end} onChange={(e) => setForm({ ...form, calling_hours_end: e.target.value })} />
              </div>
            </div>
            <button type="submit" className="btn btn-primary">Create Campaign</button>
          </form>
        </div>
      )}

      {campaigns.length === 0 ? (
        <div className="empty-state">
          <h3>No campaigns yet</h3>
          <p>Create a campaign to start outbound calling.</p>
        </div>
      ) : (
        <div className="table-container">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Status</th>
                <th>Contacts</th>
                <th>Completed</th>
                <th>Success</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {campaigns.map((c) => (
                <tr key={c.id}>
                  <td style={{ fontWeight: 500 }}>{c.name}</td>
                  <td><span className={`badge ${statusColor[c.status] || 'badge-info'}`}>{c.status}</span></td>
                  <td>{c.total_contacts}</td>
                  <td>{c.completed_contacts}</td>
                  <td>{c.successful_contacts}</td>
                  <td style={{ display: 'flex', gap: 6 }}>
                    {c.status === 'draft' && (
                      <button className="btn btn-primary" style={{ fontSize: 12, padding: '4px 10px' }}
                        onClick={() => api.startCampaign(c.id).then(load)}>Start</button>
                    )}
                    {c.status === 'running' && (
                      <button className="btn btn-secondary" style={{ fontSize: 12, padding: '4px 10px' }}
                        onClick={() => api.pauseCampaign(c.id).then(load)}>Pause</button>
                    )}
                    {c.status === 'paused' && (
                      <button className="btn btn-primary" style={{ fontSize: 12, padding: '4px 10px' }}
                        onClick={() => api.startCampaign(c.id).then(load)}>Resume</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Layout>
  );
}
