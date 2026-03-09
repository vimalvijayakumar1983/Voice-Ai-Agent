import { useEffect, useState } from 'react';
import Layout from '@/components/Layout';
import { api } from '@/lib/api';

export default function Agents() {
  const [agents, setAgents] = useState<any[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({
    name: '',
    system_prompt: '',
    greeting_message: '',
    model_provider: 'openai',
    model_name: 'gpt-4o',
    temperature: 0.7,
    language: 'en-US',
  });

  const loadAgents = () => api.listAgents().then(setAgents).catch(() => {});

  useEffect(() => { loadAgents(); }, []);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.createAgent(form);
      setShowCreate(false);
      setForm({ name: '', system_prompt: '', greeting_message: '', model_provider: 'openai', model_name: 'gpt-4o', temperature: 0.7, language: 'en-US' });
      loadAgents();
    } catch (err: any) {
      alert(err.message);
    }
  };

  return (
    <Layout>
      <div className="page-header">
        <h1>AI Agents</h1>
        <button className="btn btn-primary" onClick={() => setShowCreate(!showCreate)}>
          {showCreate ? 'Cancel' : '+ Create Agent'}
        </button>
      </div>

      {showCreate && (
        <div className="card" style={{ marginBottom: 24 }}>
          <h3 style={{ marginBottom: 16 }}>Create New Agent</h3>
          <form onSubmit={handleCreate}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
              <div className="form-group">
                <label>Agent Name</label>
                <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
              </div>
              <div className="form-group">
                <label>Model</label>
                <select value={form.model_name} onChange={(e) => setForm({ ...form, model_name: e.target.value })}>
                  <option value="gpt-4o">GPT-4o</option>
                  <option value="gpt-4o-mini">GPT-4o Mini</option>
                  <option value="claude-sonnet-4-20250514">Claude Sonnet</option>
                </select>
              </div>
            </div>
            <div className="form-group">
              <label>System Prompt</label>
              <textarea value={form.system_prompt} onChange={(e) => setForm({ ...form, system_prompt: e.target.value })} required placeholder="You are a helpful assistant that..." />
            </div>
            <div className="form-group">
              <label>Greeting Message</label>
              <input value={form.greeting_message} onChange={(e) => setForm({ ...form, greeting_message: e.target.value })} placeholder="Hello! How can I help you today?" />
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
              <div className="form-group">
                <label>Temperature ({form.temperature})</label>
                <input type="range" min="0" max="1" step="0.1" value={form.temperature} onChange={(e) => setForm({ ...form, temperature: parseFloat(e.target.value) })} />
              </div>
              <div className="form-group">
                <label>Language</label>
                <select value={form.language} onChange={(e) => setForm({ ...form, language: e.target.value })}>
                  <option value="en-US">English (US)</option>
                  <option value="en-GB">English (UK)</option>
                  <option value="es-ES">Spanish</option>
                  <option value="fr-FR">French</option>
                  <option value="de-DE">German</option>
                </select>
              </div>
            </div>
            <button type="submit" className="btn btn-primary">Create Agent</button>
          </form>
        </div>
      )}

      {agents.length === 0 ? (
        <div className="empty-state">
          <h3>No agents yet</h3>
          <p>Create your first AI voice agent to start making calls.</p>
        </div>
      ) : (
        <div className="table-container">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Model</th>
                <th>Language</th>
                <th>Status</th>
                <th>Created</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {agents.map((agent) => (
                <tr key={agent.id}>
                  <td style={{ fontWeight: 500 }}>{agent.name}</td>
                  <td>{agent.model_name}</td>
                  <td>{agent.language}</td>
                  <td>
                    <span className={`badge ${agent.is_active ? 'badge-success' : 'badge-danger'}`}>
                      {agent.is_active ? 'Active' : 'Inactive'}
                    </span>
                  </td>
                  <td style={{ color: 'var(--text-secondary)', fontSize: 13 }}>
                    {new Date(agent.created_at).toLocaleDateString()}
                  </td>
                  <td>
                    <button className="btn btn-secondary" style={{ fontSize: 12, padding: '4px 10px' }}
                      onClick={() => api.deleteAgent(agent.id).then(loadAgents)}>
                      Delete
                    </button>
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
