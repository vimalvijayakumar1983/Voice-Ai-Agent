import Link from 'next/link';
import { useEffect, useState } from 'react';
import {
  Bot,
  CheckCircle2,
  CloudUpload,
  FlaskConical,
  Globe2,
  Pencil,
  Plus,
  Radio,
  RefreshCw,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import AgentEditor, { AgentEditorValues, defaultAgentValues } from '@/components/AgentEditor';
import Layout from '@/components/Layout';
import { api, AgentProviderCatalog, ProviderStatus, VoiceAgent } from '@/lib/api';

export default function Agents() {
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [provider, setProvider] = useState<ProviderStatus | null>(null);
  const [catalog, setCatalog] = useState<AgentProviderCatalog | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [editingAgentId, setEditingAgentId] = useState<string | null>(null);
  const [working, setWorking] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  useEffect(() => {
    let active = true;
    Promise.allSettled([
      api.listAgents(),
      api.getProviderStatus(),
      api.getAgentProviderCatalog(),
    ]).then(([agentResult, providerResult, catalogResult]) => {
      if (!active) return;
      setAgents(agentResult.status === 'fulfilled' ? agentResult.value : []);
      setProvider(providerResult.status === 'fulfilled' ? providerResult.value : null);
      setCatalog(catalogResult.status === 'fulfilled' ? catalogResult.value : null);
    });
    return () => { active = false; };
  }, []);

  const editingAgent = agents.find((agent) => agent.id === editingAgentId) ?? null;

  const openCreate = () => {
    setEditingAgentId(null);
    setShowCreate(true);
    setNotice(null);
  };

  const openEdit = (agent: VoiceAgent) => {
    setShowCreate(false);
    setEditingAgentId(agent.id);
    setNotice(null);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  const saveAgent = async (values: AgentEditorValues) => {
    const actionKey = editingAgent ? `save-${editingAgent.id}` : 'save-new';
    setWorking(actionKey);
    setNotice(null);
    try {
      if (editingAgent) {
        const updated = await api.updateAgent(editingAgent.id, values);
        setAgents((current) => current.map((agent) => agent.id === updated.id ? updated : agent));
        setEditingAgentId(null);
        setNotice({
          type: 'success',
          text: `${updated.name} was updated${updated.provider_agent_id ? ' and is ready to publish' : ''}.`,
        });
      } else {
        const created = await api.createAgent(values);
        setAgents((current) => [created, ...current]);
        setShowCreate(false);
        setNotice({ type: 'success', text: `${created.name} was created as a local draft.` });
      }
    } catch (error) {
      setNotice({
        type: 'error',
        text: error instanceof Error ? error.message : 'Could not save the agent.',
      });
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
        text: providerActionNotice(agent.name, action, updated.sync_status),
      });
    } catch (error) {
      setNotice({
        type: 'error',
        text: error instanceof Error ? error.message : 'Provider action failed.',
      });
    } finally {
      setWorking(null);
    }
  };

  const removeAgent = async (agent: VoiceAgent) => {
    if (!window.confirm(`Delete ${agent.name} from this workspace? This does not archive the remote Smallest.ai agent.`)) return;
    setWorking(`delete-${agent.id}`);
    setNotice(null);
    try {
      await api.deleteAgent(agent.id);
      setAgents((current) => current.filter((item) => item.id !== agent.id));
      setNotice({ type: 'success', text: `${agent.name} was deleted from this workspace.` });
    } catch (error) {
      setNotice({
        type: 'error',
        text: error instanceof Error ? error.message : 'Could not delete the agent.',
      });
    } finally {
      setWorking(null);
    }
  };

  const resolveProviderOperation = async (agent: VoiceAgent) => {
    setNotice(null);
    let payload:
      | {
          action: 'confirm_create_absent' | 'confirm_publish_absent';
          confirmation: string;
        }
      | null = null;
    if (agent.sync_status === 'provision_unknown') {
      if (window.confirm(
        'Confirm that the Smallest.ai dashboard shows no remote agent for this operation. If one exists, cancel and contact a platform operator; workspace users cannot safely claim shared provider resources.',
      )) {
        payload = {
          action: 'confirm_create_absent',
          confirmation: 'I CONFIRM NO REMOTE AGENT EXISTS',
        };
      }
    } else if (
      agent.sync_status === 'publish_unknown'
      && window.confirm('Confirm that Smallest.ai shows no new branch revision for this publish operation?')
    ) {
      payload = {
        action: 'confirm_publish_absent',
        confirmation: 'I CONFIRM NO NEW REVISION EXISTS',
      };
    }
    if (!payload) return;

    setWorking(`resolve-${agent.id}`);
    try {
      const updated = await api.resolveSmallestAgent(agent.id, payload);
      setAgents((current) => current.map((item) => item.id === updated.id ? updated : item));
      setNotice({
        type: 'success',
        text: `${agent.name}'s provider operation was reconciled and recorded in the audit log.`,
      });
    } catch (error) {
      setNotice({
        type: 'error',
        text: error instanceof Error ? error.message : 'Could not reconcile the provider operation.',
      });
    } finally {
      setWorking(null);
    }
  };

  const closeEditor = () => {
    setShowCreate(false);
    setEditingAgentId(null);
  };

  return (
    <Layout>
      <div className="page-header">
        <div>
          <span className="page-kicker">Build & govern</span>
          <h1>Voice agents</h1>
          <p className="page-subtitle">Build multilingual Smallest.ai agents from proven templates, tune every voice, and publish only when ready.</p>
        </div>
        <div className="header-actions">
          <Link href="/playground" className="btn btn-secondary"><FlaskConical size={14} /> Open playground</Link>
          <button className="btn btn-primary" onClick={showCreate ? closeEditor : openCreate}>
            {showCreate ? <X size={14} /> : <Plus size={14} />}{showCreate ? 'Close' : 'Create agent'}
          </button>
        </div>
      </div>

      {notice && (
        <div className={`provider-alert ${notice.type === 'error' ? 'provider-alert-error' : ''}`}>
          {notice.type === 'success' ? <CheckCircle2 size={15} /> : <Radio size={15} />}{notice.text}
        </div>
      )}

      {(showCreate || editingAgent) && (
        <section className="card create-panel agent-editor-panel">
          <aside className="create-panel-aside">
            <Sparkles size={22} />
            <h3>{editingAgent ? `Edit ${editingAgent.name}` : 'Design an agent that sounds human.'}</h3>
            <p>{editingAgent ? 'Change its prompt, voice, languages, and tuning. Provisioned changes stay local until you publish.' : 'Choose a template, customize every field, then provision it when it is ready.'}</p>
            <div className="catalog-stats">
              <div><strong>{catalog?.voices.length ?? '—'}</strong><span>voices</span></div>
              <div><strong>{catalog?.languages.length ?? '—'}</strong><span>languages</span></div>
              <div><strong>{catalog?.templates.length ?? '—'}</strong><span>templates</span></div>
            </div>
          </aside>
          <AgentEditor
            key={editingAgent?.id ?? 'create'}
            mode={editingAgent ? 'edit' : 'create'}
            catalog={catalog}
            initialValues={editingAgent ? editorValues(editingAgent) : defaultAgentValues}
            busy={working === (editingAgent ? `save-${editingAgent.id}` : 'save-new')}
            onCancel={closeEditor}
            onSubmit={saveAgent}
          />
        </section>
      )}

      {agents.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state-icon"><Bot size={23} /></div>
          <h3>No voice agents yet</h3>
          <p>Choose a ready-made template, select languages and a Smallest.ai voice, then customize it for your business.</p>
          <button className="btn btn-primary" onClick={openCreate}><Plus size={14} /> Create first agent</button>
        </div>
      ) : (
        <div className="agent-grid">
          {agents.map((agent) => (
            <article className="agent-card" key={agent.id}>
              <div className="agent-card-top">
                <div className="agent-avatar"><Bot size={19} /></div>
                <div className="agent-card-title">
                  <h3>{agent.name}</h3>
                  <p>{agent.provider_agent_id ? `Atoms ID · ${agent.provider_agent_id.slice(0, 12)}…` : 'Local draft · not provisioned'}</p>
                </div>
                <button className="icon-button" disabled={providerOperationUnresolved(agent.sync_status)} onClick={() => openEdit(agent)} aria-label={`Edit ${agent.name}`}><Pencil size={15} /></button>
              </div>
              <p className="agent-card-body">{agent.description || agent.system_prompt}</p>
              <div className="agent-card-meta">
                <span className="meta-chip"><Globe2 size={9} /> {languageLabel(agent.language, catalog)}</span>
                {agent.supported_languages.length > 1 && <span className="meta-chip">+{agent.supported_languages.length - 1} languages</span>}
                <span className="meta-chip">{voiceLabel(agent.voice_id, catalog)}</span>
                <span className={`badge ${syncBadge(agent.sync_status)}`}>{agent.sync_status.replace('_', ' ')}</span>
              </div>
              <div className="agent-card-actions">
                <button className="btn btn-secondary btn-sm" disabled={providerOperationUnresolved(agent.sync_status)} onClick={() => openEdit(agent)}><Pencil size={12} /> Edit</button>
                {agent.provider_agent_id ? (
                  <button className="btn btn-secondary btn-sm" disabled={working === `sync-${agent.id}` || agent.sync_status === 'synced'} onClick={() => runAgentAction(agent, 'sync')}><RefreshCw size={12} /> {providerActionLabel(agent.sync_status)}</button>
                ) : agent.sync_status === 'provision_unknown' ? (
                  <button className="btn btn-primary btn-sm" disabled={working === `resolve-${agent.id}`} onClick={() => resolveProviderOperation(agent)}><RefreshCw size={12} /> Resolve create</button>
                ) : agent.sync_status === 'provisioning' ? (
                  <button className="btn btn-secondary btn-sm" disabled={working === `provision-${agent.id}`} onClick={() => runAgentAction(agent, 'provision')}><RefreshCw size={12} /> Check status</button>
                ) : (
                  <button className="btn btn-primary btn-sm" disabled={!provider?.configured || !provider?.webhook_configured || providerOperationUnresolved(agent.sync_status) || working === `provision-${agent.id}`} onClick={() => runAgentAction(agent, 'provision')}><CloudUpload size={12} /> Provision</button>
                )}
                {agent.sync_status === 'publish_unknown' && (
                  <button className="btn btn-secondary btn-sm" disabled={working === `resolve-${agent.id}`} onClick={() => resolveProviderOperation(agent)}><RefreshCw size={12} /> Resolve unknown</button>
                )}
                {agent.last_synced_at ? (
                  <Link href={`/playground?agent=${agent.id}`} className="btn btn-secondary btn-sm"><FlaskConical size={12} /> Test</Link>
                ) : (
                  <button className="btn btn-secondary btn-sm" disabled><FlaskConical size={12} /> Test</button>
                )}
                <button className="btn btn-ghost btn-sm" disabled={Boolean(agent.provider_agent_id) || providerOperationUnresolved(agent.sync_status) || working === `delete-${agent.id}`} onClick={() => removeAgent(agent)} aria-label={`Delete ${agent.name}`} title={agent.provider_agent_id ? 'Provisioned agents must be archived with their provider resource.' : undefined}><Trash2 size={12} /></button>
              </div>
            </article>
          ))}
        </div>
      )}
    </Layout>
  );
}

function editorValues(agent: VoiceAgent): AgentEditorValues {
  return {
    name: agent.name,
    description: agent.description ?? '',
    system_prompt: agent.system_prompt,
    greeting_message: agent.greeting_message ?? '',
    model_provider: agent.model_provider,
    model_name: agent.model_name,
    voice_provider: agent.voice_provider,
    voice_id: agent.voice_id,
    temperature: agent.temperature,
    language: agent.language,
    supported_languages: agent.supported_languages,
    speech_rate: agent.speech_rate,
    timezone: agent.timezone,
  };
}

function languageLabel(code: string, catalog: AgentProviderCatalog | null) {
  return catalog?.languages.find((language) => language.code === code)?.name ?? code.toUpperCase();
}

function voiceLabel(id: string, catalog: AgentProviderCatalog | null) {
  if (!id) return 'Default voice';
  return catalog?.voices.find((voice) => voice.id === id)?.name ?? id;
}

function syncBadge(status: VoiceAgent['sync_status']) {
  if (status === 'synced') return 'badge-success';
  if (status === 'error') return 'badge-danger';
  if (status === 'dirty' || providerOperationUnresolved(status)) return 'badge-warning';
  return 'badge-neutral';
}

function providerOperationUnresolved(status: VoiceAgent['sync_status']) {
  return ['provisioning', 'provision_unknown', 'publishing', 'provider_scanning', 'publish_unknown'].includes(status);
}

function providerActionLabel(status: VoiceAgent['sync_status']) {
  if (status === 'synced') return 'In sync';
  if (['publishing', 'provider_scanning', 'publish_unknown'].includes(status)) return 'Check status';
  return 'Publish';
}

function providerActionNotice(
  name: string,
  action: 'provision' | 'sync',
  status: VoiceAgent['sync_status'],
) {
  if (status === 'provider_scanning' || status === 'publish_unknown') {
    return `${name} is awaiting Smallest.ai revision and security checks. Use Check status before retrying.`;
  }
  if (status === 'dirty') {
    return `${name}'s interrupted provider update is safe to publish again.`;
  }
  return action === 'provision'
    ? `${name} is provisioned and published on Smallest.ai.`
    : `${name} has been verified through the Smallest.ai versioning workflow.`;
}
