import Link from 'next/link';
import { FormEvent, useEffect, useState } from 'react';
import {
  Bot,
  CheckCircle2,
  CloudUpload,
  FlaskConical,
  Globe2,
  MoreHorizontal,
  Plus,
  Radio,
  RefreshCw,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import Layout from '@/components/Layout';
import { api, ProviderStatus, VoiceAgent } from '@/lib/api';

const initialForm = {
  name: '',
  description: '',
  system_prompt: '',
  greeting_message: '',
  model_provider: 'smallest',
  model_name: 'electron',
  voice_provider: 'smallest',
  voice_id: '',
  temperature: 0.7,
  language: 'en',
  speech_rate: 1,
  timezone: 'Asia/Dubai',
};

export default function Agents() {
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [provider, setProvider] = useState<ProviderStatus | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState(initialForm);
  const [working, setWorking] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  const loadAgents = () => api.listAgents().then(setAgents).catch(() => setAgents([]));

  useEffect(() => {
    loadAgents();
    api.getProviderStatus().then(setProvider).catch(() => setProvider(null));
  }, []);

  const handleCreate = async (event: FormEvent) => {
    event.preventDefault();
    setWorking('create');
    setNotice(null);
    try {
      const created = await api.createAgent(form);
      setAgents((current) => [created, ...current]);
      setForm(initialForm);
      setShowCreate(false);
      setNotice({ type: 'success', text: `${created.name} was created as a local draft.` });
    } catch (error) {
      setNotice({ type: 'error', text: error instanceof Error ? error.message : 'Could not create the agent.' });
    } finally {
      setWorking(null);
    }
  };

  const runAgentAction = async (agent: VoiceAgent, action: 'provision' | 'sync') => {
    setWorking(`${action}-${agent.id}`);
    setNotice(null);
    try {
      const updated = action === 'provision'
        ? await api.provisionSmallestAgent(agent.id)
        : await api.syncSmallestAgent(agent.id);
      setAgents((current) => current.map((item) => item.id === updated.id ? updated : item));
      setNotice({
        type: 'success',
        text: action === 'provision'
          ? `${agent.name} is now provisioned on Smallest.ai.`
          : `${agent.name} has been published through the Smallest.ai versioning workflow.`,
      });
    } catch (error) {
      setNotice({ type: 'error', text: error instanceof Error ? error.message : 'Provider action failed.' });
    } finally {
      setWorking(null);
    }
  };

  const removeAgent = async (agent: VoiceAgent) => {
    if (!window.confirm(`Delete ${agent.name} from this workspace? This does not archive the remote Smallest.ai agent.`)) return;
    setWorking(`delete-${agent.id}`);
    await api.deleteAgent(agent.id);
    setAgents((current) => current.filter((item) => item.id !== agent.id));
    setWorking(null);
  };

  return (
    <Layout>
      <div className="page-header">
        <div>
          <span className="page-kicker">Build & govern</span>
          <h1>Voice agents</h1>
          <p className="page-subtitle">Create production-grade agents, control what reaches Smallest.ai, and test every change before customer calls.</p>
        </div>
        <div className="header-actions">
          <Link href="/playground" className="btn btn-secondary"><FlaskConical size={14} /> Open playground</Link>
          <button className="btn btn-primary" onClick={() => setShowCreate((visible) => !visible)}>
            {showCreate ? <X size={14} /> : <Plus size={14} />}{showCreate ? 'Close' : 'Create agent'}
          </button>
        </div>
      </div>

      {notice && <div className={`provider-alert ${notice.type === 'error' ? 'badge-danger' : ''}`}>{notice.type === 'success' ? <CheckCircle2 size={15} /> : <Radio size={15} />}{notice.text}</div>}

      {showCreate && (
        <section className="card create-panel">
          <aside className="create-panel-aside">
            <Sparkles size={22} />
            <h3>Design an agent that sounds human.</h3>
            <p>Start locally, review the instructions, then provision it on Smallest.ai when it is ready.</p>
            <div className="create-steps">
              <div className="create-step active"><span>1</span> Identity & objective</div>
              <div className="create-step"><span>2</span> Voice & language</div>
              <div className="create-step"><span>3</span> Test & publish</div>
            </div>
          </aside>
          <form className="create-panel-form" onSubmit={handleCreate}>
            <div className="provider-alert"><CloudUpload size={15} /> The API key stays server-side. Creating this form does not call Smallest.ai.</div>
            <div className="form-grid">
              <div className="form-group"><label>Agent name</label><input required value={form.name} placeholder="e.g. Al Zaabi Receptionist" onChange={(event) => setForm({ ...form, name: event.target.value })} /></div>
              <div className="form-group"><label>Purpose</label><input value={form.description} placeholder="Appointments, sales, support…" onChange={(event) => setForm({ ...form, description: event.target.value })} /></div>
            </div>
            <div className="form-group"><label>System prompt <span>{form.system_prompt.length} characters</span></label><textarea required value={form.system_prompt} placeholder="Define the role, goal, conversation flow, tool rules, escalation path, and guardrails…" onChange={(event) => setForm({ ...form, system_prompt: event.target.value })} /><p className="form-hint">Use short spoken responses, ask one question at a time, confirm important information, and never invent customer data.</p></div>
            <div className="form-group"><label>First message</label><input value={form.greeting_message} placeholder="Hello, you’re speaking with… How can I help?" onChange={(event) => setForm({ ...form, greeting_message: event.target.value })} /></div>
            <div className="form-grid">
              <div className="form-group"><label>Model</label><select value={form.model_name} onChange={(event) => setForm({ ...form, model_name: event.target.value })}><option value="electron">Electron — voice optimized</option></select></div>
              <div className="form-group"><label>Primary language</label><select value={form.language} onChange={(event) => setForm({ ...form, language: event.target.value })}><option value="en">English</option><option value="ar">Arabic</option><option value="hi">Hindi</option><option value="ml">Malayalam</option></select></div>
              <div className="form-group"><label>Smallest voice ID <span>Optional</span></label><input value={form.voice_id} placeholder="Use platform default" onChange={(event) => setForm({ ...form, voice_id: event.target.value })} /></div>
              <div className="form-group"><label>Timezone</label><select value={form.timezone} onChange={(event) => setForm({ ...form, timezone: event.target.value })}><option value="Asia/Dubai">UAE — Asia/Dubai</option><option value="Asia/Kolkata">India — Asia/Kolkata</option><option value="Europe/London">UK — Europe/London</option></select></div>
            </div>
            <div className="header-actions"><button type="button" className="btn btn-secondary" onClick={() => setShowCreate(false)}>Cancel</button><button type="submit" className="btn btn-primary" disabled={working === 'create'}>{working === 'create' ? <RefreshCw size={14} /> : <Plus size={14} />}{working === 'create' ? 'Creating…' : 'Create local draft'}</button></div>
          </form>
        </section>
      )}

      {agents.length === 0 ? (
        <div className="empty-state"><div className="empty-state-icon"><Bot size={23} /></div><h3>No voice agents yet</h3><p>Create the first agent locally, validate its behavior in the playground, and publish only when you are comfortable.</p><button className="btn btn-primary" onClick={() => setShowCreate(true)}><Plus size={14} /> Create first agent</button></div>
      ) : (
        <div className="agent-grid">
          {agents.map((agent) => (
            <article className="agent-card" key={agent.id}>
              <div className="agent-card-top"><div className="agent-avatar"><Bot size={19} /></div><div className="agent-card-title"><h3>{agent.name}</h3><p>{agent.provider_agent_id ? `Atoms ID · ${agent.provider_agent_id.slice(0, 12)}…` : 'Local draft · not provisioned'}</p></div><button className="icon-button" aria-label={`More actions for ${agent.name}`}><MoreHorizontal size={16} /></button></div>
              <p className="agent-card-body">{agent.description || agent.system_prompt}</p>
              <div className="agent-card-meta"><span className="meta-chip"><Globe2 size={9} /> {agent.language.toUpperCase()}</span><span className="meta-chip">{agent.model_name}</span><span className={`badge ${syncBadge(agent.sync_status)}`}>{agent.sync_status.replace('_', ' ')}</span></div>
              <div className="agent-card-actions">
                {agent.provider_agent_id ? <button className="btn btn-secondary btn-sm" disabled={working === `sync-${agent.id}` || agent.sync_status === 'synced'} onClick={() => runAgentAction(agent, 'sync')}><RefreshCw size={12} /> {agent.sync_status === 'synced' ? 'In sync' : 'Publish'}</button> : <button className="btn btn-primary btn-sm" disabled={!provider?.configured || working === `provision-${agent.id}`} onClick={() => runAgentAction(agent, 'provision')}><CloudUpload size={12} /> Provision</button>}
                <Link href={`/playground?agent=${agent.id}`} className="btn btn-secondary btn-sm"><FlaskConical size={12} /> Test</Link>
                <button className="btn btn-ghost btn-sm" disabled={working === `delete-${agent.id}`} onClick={() => removeAgent(agent)} aria-label={`Delete ${agent.name}`}><Trash2 size={12} /></button>
              </div>
            </article>
          ))}
        </div>
      )}
    </Layout>
  );
}

function syncBadge(status: VoiceAgent['sync_status']) {
  if (status === 'synced') return 'badge-success';
  if (status === 'error') return 'badge-danger';
  if (status === 'dirty' || status === 'publishing') return 'badge-warning';
  return 'badge-neutral';
}
