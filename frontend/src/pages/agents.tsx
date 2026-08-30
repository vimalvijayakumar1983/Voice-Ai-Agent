import Link from 'next/link';
import { useEffect, useMemo, useState } from 'react';
import {
  ArrowDownAZ,
  Bot,
  CheckCircle2,
  CircleAlert,
  CloudUpload,
  FlaskConical,
  Globe2,
  Loader2,
  Pencil,
  Plus,
  Radio,
  RefreshCw,
  Search,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import AgentEditor, { AgentEditorValues, defaultAgentValues } from '@/components/AgentEditor';
import RuntimeControlPanel from '@/components/RuntimeControlPanel';
import agentEditorDiff from '@/components/agent-editor-diff.cjs';
import Layout from '@/components/Layout';
import {
  agentTestReadinessMessage,
  isAgentCallReady,
  isProviderConfigCorrection,
  providerActionLabel,
  providerActionNotice,
} from '@/lib/agent-readiness.cjs';
import { api, AgentProviderCatalog, ProviderStatus, RuntimeProfile, VoiceAgent } from '@/lib/api';

const { agentEditorPatch, agentUpdateNotice, requiresSmallestDeprovision } = agentEditorDiff;

type AgentLoadErrors = Partial<Record<'agents' | 'provider' | 'catalog' | 'runtime', string>>;
type DeploymentFilter = 'all' | 'local' | 'synced' | 'changes' | 'attention';
type AgentSort = 'updated' | 'created' | 'name-asc' | 'name-desc';

export default function Agents() {
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [provider, setProvider] = useState<ProviderStatus | null>(null);
  const [catalog, setCatalog] = useState<AgentProviderCatalog | null>(null);
  const [runtimeProfiles, setRuntimeProfiles] = useState<Record<string, RuntimeProfile>>({});
  const [runtimeAgentId, setRuntimeAgentId] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [editingAgentId, setEditingAgentId] = useState<string | null>(null);
  const [working, setWorking] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ type: 'success' | 'info' | 'error'; text: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadErrors, setLoadErrors] = useState<AgentLoadErrors>({});
  const [reloadKey, setReloadKey] = useState(0);
  const [query, setQuery] = useState('');
  const [deploymentFilter, setDeploymentFilter] = useState<DeploymentFilter>('all');
  const [languageFilter, setLanguageFilter] = useState('all');
  const [sort, setSort] = useState<AgentSort>('updated');

  useEffect(() => {
    let active = true;
    Promise.allSettled([
      api.listAgents(),
      api.getProviderStatus(),
      api.getAgentProviderCatalog(),
      api.listRuntimeProfiles(),
    ]).then(([agentResult, providerResult, catalogResult, runtimeResult]) => {
      if (!active) return;
      const errors: AgentLoadErrors = {};
      if (agentResult.status === 'fulfilled') setAgents(agentResult.value);
      else {
        setAgents([]);
        errors.agents = errorMessage(agentResult.reason, 'Could not load agents.');
      }
      if (providerResult.status === 'fulfilled') setProvider(providerResult.value);
      else {
        setProvider(null);
        errors.provider = errorMessage(providerResult.reason, 'Could not load provider status.');
      }
      if (catalogResult.status === 'fulfilled') setCatalog(catalogResult.value);
      else {
        setCatalog(null);
        errors.catalog = errorMessage(catalogResult.reason, 'Could not load the current voice and language catalog.');
      }
      if (runtimeResult.status === 'fulfilled') {
        setRuntimeProfiles(Object.fromEntries(runtimeResult.value.map((profile) => [profile.agent_id, profile])));
      } else {
        setRuntimeProfiles({});
        errors.runtime = errorMessage(runtimeResult.reason, 'Could not load VAV runtime controls.');
      }
      setLoadErrors(errors);
      setLoading(false);
    });
    return () => { active = false; };
  }, [reloadKey]);

  const editingAgent = agents.find((agent) => agent.id === editingAgentId) ?? null;
  const runtimeAgent = agents.find((agent) => agent.id === runtimeAgentId) ?? null;
  const runtimeProfile = runtimeAgentId ? runtimeProfiles[runtimeAgentId] ?? null : null;
  const languageOptions = useMemo(() => {
    const codes = new Set(agents.flatMap((agent) => agent.supported_languages));
    return Array.from(codes).sort((left, right) => languageLabel(left, catalog).localeCompare(languageLabel(right, catalog)));
  }, [agents, catalog]);
  const filteredAgents = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    const matches = agents.filter((agent) => {
      const searchable = [
        agent.name,
        agent.description,
        agent.system_prompt,
        agent.provider_agent_id,
        agent.provider_revision_id,
        agent.voice_id,
        voiceLabel(agent.voice_id, catalog),
        ...agent.supported_languages,
        ...agent.supported_languages.map((code) => languageLabel(code, catalog)),
      ].filter(Boolean).join(' ').toLowerCase();
      const deploymentMatches = deploymentFilter === 'all'
        || (deploymentFilter === 'local' && !agent.provider_agent_id)
        || (deploymentFilter === 'synced' && agent.sync_status === 'synced')
        || (deploymentFilter === 'changes' && agent.sync_status === 'dirty')
        || (deploymentFilter === 'attention' && (
          agent.sync_status === 'error' || providerOperationUnresolved(agent.sync_status)
        ));
      return (!normalizedQuery || searchable.includes(normalizedQuery))
        && deploymentMatches
        && (languageFilter === 'all' || agent.supported_languages.includes(languageFilter));
    });
    return [...matches].sort((left, right) => {
      if (sort === 'name-asc') return left.name.localeCompare(right.name);
      if (sort === 'name-desc') return right.name.localeCompare(left.name);
      if (sort === 'created') return Date.parse(right.created_at) - Date.parse(left.created_at);
      return Date.parse(right.updated_at) - Date.parse(left.updated_at);
    });
  }, [agents, catalog, deploymentFilter, languageFilter, query, sort]);
  const usableVoiceCount = catalog?.voices.filter((voice) => (
    Boolean(voice.synthesizer_model) && !voice.unavailability_reason
  )).length;
  const catalogReady = Boolean(catalog && catalog.voices.length && catalog.languages.length);

  const openCreate = () => {
    if (!catalogReady) {
      setNotice({ type: 'error', text: 'The provider catalog must load before a voice or language configuration can be created safely.' });
      return;
    }
    setEditingAgentId(null);
    setShowCreate(true);
    setNotice(null);
  };

  const openEdit = (agent: VoiceAgent) => {
    setShowCreate(false);
    setEditingAgentId(agent.id);
    setNotice(catalogReady ? null : {
      type: 'info',
      text: 'The provider catalog is unavailable. Existing voice and language settings are locked, but unrelated fields remain editable.',
    });
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  const refreshVoiceCatalog = async () => {
    try {
      const refreshed = await api.getAgentProviderCatalog();
      setCatalog(refreshed);
      setLoadErrors((current) => {
        const next = { ...current };
        delete next.catalog;
        return next;
      });
    } catch (error) {
      const message = errorMessage(error, 'Could not refresh the voice catalog.');
      setLoadErrors((current) => ({ ...current, catalog: message }));
      throw error;
    }
  };

  const saveAgent = async (values: AgentEditorValues) => {
    const actionKey = editingAgent ? `save-${editingAgent.id}` : 'save-new';
    setWorking(actionKey);
    setNotice(null);
    try {
      if (editingAgent) {
        const patch = agentEditorPatch(editorValues(editingAgent), values);
        if (Object.keys(patch).length === 0) {
          setEditingAgentId(null);
          setNotice({ type: 'info', text: `${editingAgent.name} has no changes to save.` });
          return;
        }
        const deprovisionExistingProvider = requiresSmallestDeprovision(editingAgent, patch);
        if (deprovisionExistingProvider && !window.confirm(
          `Switch ${editingAgent.name} from Smallest.ai to Sarvam? The live Smallest.ai remote agent will be permanently archived first. The VAV agent and its knowledge base will be preserved.`,
        )) return;
        const updated = await api.updateAgent(editingAgent.id, patch, {
          deprovisionExistingProvider,
        });
        setAgents((current) => current.map((agent) => agent.id === updated.id ? updated : agent));
        setEditingAgentId(null);
        setNotice({
          type: 'success',
          text: deprovisionExistingProvider
            ? `${updated.name} was archived on Smallest.ai and switched to Sarvam. Its VAV configuration and knowledge binding were preserved.`
            : agentUpdateNotice(updated.name, updated.sync_status),
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
    const correctionVerification = action === 'sync' && isProviderConfigCorrection(agent);
    try {
      const updated = action === 'provision'
        ? await api.provisionSmallestAgent(agent.id)
        : await api.syncSmallestAgent(agent.id);
      setAgents((current) => current.map((item) => item.id === updated.id ? updated : item));
      setNotice(providerActionNotice(agent.name, action, updated.sync_status, correctionVerification));
    } catch (error) {
      try {
        const refreshed = await api.getAgent(agent.id);
        setAgents((current) => current.map((item) => item.id === refreshed.id ? refreshed : item));
      } catch {
        // Preserve the original provider error when a best-effort state refresh also fails.
      }
      setNotice({
        type: 'error',
        text: error instanceof Error ? error.message : 'Provider action failed.',
      });
    } finally {
      setWorking(null);
    }
  };

  const removeAgent = async (agent: VoiceAgent) => {
    const providerNotice = agent.provider_agent_id
      ? ' Its Smallest.ai agent will also be archived.'
      : '';
    if (!window.confirm(`Permanently delete ${agent.name}?${providerNotice} Its knowledge base will not be deleted.`)) return;
    setWorking(`delete-${agent.id}`);
    setNotice(null);
    try {
      await api.deleteAgent(agent.id);
      setAgents((current) => current.filter((item) => item.id !== agent.id));
      setRuntimeProfiles((current) => {
        const next = { ...current };
        delete next[agent.id];
        return next;
      });
      if (runtimeAgentId === agent.id) setRuntimeAgentId(null);
      setNotice({
        type: 'success',
        text: agent.provider_agent_id
          ? `${agent.name} was deleted from VAV and archived on Smallest.ai.`
          : `${agent.name} was deleted from this workspace.`,
      });
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

  const retryLoad = () => {
    setShowCreate(false);
    setEditingAgentId(null);
    setLoading(true);
    setLoadErrors({});
    setAgents([]);
    setProvider(null);
    setCatalog(null);
    setRuntimeProfiles({});
    setRuntimeAgentId(null);
    setReloadKey((value) => value + 1);
  };

  return (
    <Layout>
      <div className="page-header">
        <div>
          <span className="page-kicker">Build & govern</span>
          <h1>Voice agents</h1>
          <p className="page-subtitle">Create and review Smallest.ai agent configurations. Provider sync is separate from local editing.</p>
        </div>
        <div className="header-actions">
          <Link href="/playground" className="btn btn-secondary"><FlaskConical size={14} /> Open playground</Link>
          <button
            className="btn btn-primary"
            onClick={showCreate ? closeEditor : openCreate}
            disabled={loading || (!showCreate && !catalogReady)}
            title={!loading && !catalogReady ? 'Retry the provider catalog before creating an agent.' : undefined}
          >
            {showCreate ? <X size={14} /> : <Plus size={14} />}{showCreate ? 'Close' : 'Create agent'}
          </button>
        </div>
      </div>

      {notice && (
        <div
          className={`provider-alert ${notice.type === 'error' ? 'provider-alert-error' : ''}`}
          role={notice.type === 'error' ? 'alert' : 'status'}
          aria-live={notice.type === 'error' ? 'assertive' : 'polite'}
        >
          {notice.type === 'success' ? <CheckCircle2 size={15} /> : notice.type === 'error' ? <CircleAlert size={15} /> : <Radio size={15} />}
          <span>{notice.text}</span>
        </div>
      )}

      {runtimeAgent && runtimeProfile ? (
        <RuntimeControlPanel
          key={`${runtimeProfile.agent_id}:${runtimeProfile.updated_at ?? 'draft'}`}
          agent={runtimeAgent}
          profile={runtimeProfile}
          onClose={() => setRuntimeAgentId(null)}
          onChange={(next) => setRuntimeProfiles((current) => ({ ...current, [next.agent_id]: next }))}
        />
      ) : null}

      {loading && (
        <div className="page-loading" role="status" aria-live="polite">
          <Loader2 className="spin" size={16} /> Loading agents and provider capabilities…
        </div>
      )}

      {!loading && Object.keys(loadErrors).length > 0 && (
        <div className="provider-alert provider-alert-error" role="alert">
          <CircleAlert size={15} />
          <span>
            {Object.entries(loadErrors).map(([area, message]) => `${area}: ${message}`).join(' ')}
            {' '}Unavailable data is not treated as an empty workspace or a supported capability.
          </span>
          <button type="button" className="btn btn-secondary btn-sm" onClick={retryLoad}>
            <RefreshCw size={12} /> Retry
          </button>
        </div>
      )}

      {(showCreate || editingAgent) && (
        <section className="card create-panel agent-editor-panel">
          <aside className="create-panel-aside">
            <Sparkles size={22} />
            <h3>{editingAgent ? `Edit ${editingAgent.name}` : 'Create a local agent draft.'}</h3>
            <p>{editingAgent ? 'Changes to a provisioned agent remain local until a provider publish succeeds.' : 'Choose a built-in starting point, review every field, then provision deliberately.'}</p>
            <div className="catalog-stats">
              <div><strong>{usableVoiceCount ?? '—'}</strong><span>usable catalog voices</span></div>
              <div><strong>{catalog?.languages.length ?? '—'}</strong><span>catalog languages</span></div>
              <div><strong>{catalog?.templates.length ?? '—'}</strong><span>built-in templates</span></div>
            </div>
          </aside>
          <AgentEditor
            key={editingAgent?.id ?? 'create'}
            mode={editingAgent ? 'edit' : 'create'}
            catalog={catalog}
            catalogError={loadErrors.catalog ?? null}
            initialValues={editingAgent ? editorValues(editingAgent) : defaultAgentValues}
            busy={working === (editingAgent ? `save-${editingAgent.id}` : 'save-new')}
            onCancel={closeEditor}
            onSubmit={saveAgent}
            onCatalogRefresh={refreshVoiceCatalog}
          />
        </section>
      )}

      {!loading && !loadErrors.agents && agents.length > 0 && (
        <section className="card" aria-label="Filter voice agents">
          <div className="form-grid">
            <div className="form-group">
              <label htmlFor="agent-search">Search agents</label>
              <div className="voice-search-control">
                <Search size={14} aria-hidden="true" />
                <input
                  id="agent-search"
                  type="search"
                  value={query}
                  placeholder="Name, voice, language, provider ID…"
                  onChange={(event) => setQuery(event.target.value)}
                />
              </div>
            </div>
            <div className="form-group">
              <label htmlFor="agent-deployment-filter">Deployment state</label>
              <select id="agent-deployment-filter" value={deploymentFilter} onChange={(event) => setDeploymentFilter(event.target.value as DeploymentFilter)}>
                <option value="all">All deployment states</option>
                <option value="local">Local drafts</option>
                <option value="synced">Provider synced</option>
                <option value="changes">Unpublished local changes</option>
                <option value="attention">Provider attention required</option>
              </select>
            </div>
            <div className="form-group">
              <label htmlFor="agent-language-filter">Configured language</label>
              <select id="agent-language-filter" value={languageFilter} onChange={(event) => setLanguageFilter(event.target.value)}>
                <option value="all">All configured languages</option>
                {languageOptions.map((code) => <option value={code} key={code}>{languageLabel(code, catalog)}</option>)}
              </select>
            </div>
            <div className="form-group">
              <label htmlFor="agent-sort"><ArrowDownAZ size={12} aria-hidden="true" /> Sort</label>
              <select id="agent-sort" value={sort} onChange={(event) => setSort(event.target.value as AgentSort)}>
                <option value="updated">Recently updated</option>
                <option value="created">Recently created</option>
                <option value="name-asc">Name A–Z</option>
                <option value="name-desc">Name Z–A</option>
              </select>
            </div>
          </div>
          <p className="form-hint" role="status">Showing {filteredAgents.length} of {agents.length} agents.</p>
        </section>
      )}

      {!loading && !loadErrors.agents && agents.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state-icon"><Bot size={23} /></div>
          <h3>No voice agents yet</h3>
          <p>Create a local draft from a built-in template, then validate its voice and language configuration before provisioning.</p>
          <button className="btn btn-primary" onClick={openCreate} disabled={!catalogReady}><Plus size={14} /> Create first agent</button>
        </div>
      ) : !loading && !loadErrors.agents && agents.length > 0 && filteredAgents.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state-icon"><Search size={23} /></div>
          <h3>No agents match these filters</h3>
          <p>Clear the search or select broader deployment and language filters.</p>
          <button type="button" className="btn btn-secondary" onClick={() => { setQuery(''); setDeploymentFilter('all'); setLanguageFilter('all'); }}>
            Clear filters
          </button>
        </div>
      ) : !loading && !loadErrors.agents && filteredAgents.length > 0 ? (
        <div className="agent-grid">
          {filteredAgents.map((agent) => (
            <article className="agent-card" key={agent.id}>
              <div className="agent-card-top">
                <div className="agent-avatar"><Bot size={19} /></div>
                <div className="agent-card-title">
                  <h3>{agent.name}</h3>
                  <p>{deploymentDescription(agent)}</p>
                </div>
                <button className="icon-button" disabled={!catalogReady || providerOperationUnresolved(agent.sync_status)} onClick={() => openEdit(agent)} aria-label={`Edit ${agent.name}`}><Pencil size={15} /></button>
              </div>
              <p className="agent-card-body">{agent.description || agent.system_prompt}</p>
              <div className="agent-card-meta">
                <span className="meta-chip"><Globe2 size={9} /> Primary: {languageLabel(agent.language, catalog)}</span>
                <span className="meta-chip">{languageConfigurationLabel(agent)}</span>
                <span className="meta-chip">Voice: {voiceLabel(agent.voice_id, catalog)}</span>
                <span className="meta-chip">Provider: {agent.voice_provider === 'sarvam' ? 'Sarvam AI' : 'Smallest.ai'}</span>
                <span className={`badge ${syncBadge(agent.sync_status)}`}>{syncStatusLabel(agent.sync_status)}</span>
                {agent.provider_revision_id && <span className="meta-chip">Revision: {agent.provider_revision_id.slice(0, 12)}…</span>}
                {agent.last_synced_at && <span className="meta-chip">Last sync: {new Date(agent.last_synced_at).toLocaleString()}</span>}
              </div>
              <div className="agent-card-actions">
                <button className="btn btn-secondary btn-sm" disabled={!catalogReady || providerOperationUnresolved(agent.sync_status)} onClick={() => openEdit(agent)}><Pencil size={12} /> Edit</button>
                {agent.voice_provider === 'sarvam' ? (
                  <button
                    className={`btn btn-sm ${runtimeProfiles[agent.id]?.enabled ? 'btn-primary' : 'btn-secondary'}`}
                    disabled={!runtimeProfiles[agent.id]}
                    onClick={() => { setRuntimeAgentId(agent.id); window.scrollTo({ top: 0, behavior: 'smooth' }); }}
                    title={runtimeProfiles[agent.id]?.enabled ? 'Runtime active' : 'Configure and test the VAV runtime'}
                  ><Radio size={12} /> {runtimeProfiles[agent.id]?.enabled ? 'Runtime active' : 'Configure runtime'}</button>
                ) : agent.provider_agent_id ? (
                  <button className="btn btn-secondary btn-sm" disabled={working === `sync-${agent.id}` || agent.sync_status === 'synced'} onClick={() => runAgentAction(agent, 'sync')} title={isProviderConfigCorrection(agent) ? 'Recheck Smallest.ai without publishing another revision.' : undefined}><RefreshCw size={12} /> {providerActionLabel(agent)}</button>
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
                {isAgentCallReady(agent, runtimeProfiles[agent.id]) ? (
                  <Link href={`/playground?agent=${agent.id}`} className="btn btn-secondary btn-sm"><FlaskConical size={12} /> Test</Link>
                ) : (
                  <button className="btn btn-secondary btn-sm" disabled title={agentTestReadinessMessage(agent, runtimeProfiles[agent.id])}><FlaskConical size={12} /> Test</button>
                )}
                <button className="btn btn-ghost btn-sm" disabled={providerOperationUnresolved(agent.sync_status) || working === `delete-${agent.id}`} onClick={() => removeAgent(agent)} aria-label={`Delete ${agent.name}`} title={agent.provider_agent_id ? 'Delete from VAV and archive the Smallest.ai agent.' : 'Delete local draft.'}><Trash2 size={12} /></button>
              </div>
            </article>
          ))}
        </div>
      ) : null}
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
    language_switching_enabled: agent.language_switching_enabled,
    language_switching_mode: agent.language_switching_mode,
    speech_rate: agent.speech_rate,
    timezone: agent.timezone,
  };
}

function languageLabel(code: string, catalog: AgentProviderCatalog | null) {
  return catalog?.languages.find((language) => language.code === code)?.name ?? code.toUpperCase();
}

function voiceLabel(id: string, catalog: AgentProviderCatalog | null) {
  if (!id) return 'Provider default';
  const voice = catalog?.voices.find((item) => item.id === id);
  if (voice) return voice.name;
  return catalog ? `${id} · not in current catalog` : `${id} · catalog unavailable`;
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

function syncStatusLabel(status: VoiceAgent['sync_status']) {
  const labels: Record<VoiceAgent['sync_status'], string> = {
    local_only: 'Local draft',
    dirty: 'Unpublished changes',
    provisioning: 'Provisioning',
    provision_unknown: 'Create status unknown',
    publishing: 'Publishing',
    provider_scanning: 'Provider review',
    publish_unknown: 'Publish status unknown',
    synced: 'Provider synced',
    error: 'Provider error',
  };
  return labels[status];
}

function deploymentDescription(agent: VoiceAgent) {
  if (agent.voice_provider === 'sarvam') return 'Sarvam AI · VAV realtime runtime';
  if (!agent.provider_agent_id) return 'Local draft · not provisioned';
  const providerId = `Atoms ID · ${agent.provider_agent_id.slice(0, 12)}…`;
  if (agent.sync_status === 'synced') return `${providerId} · published revision recorded`;
  if (agent.sync_status === 'dirty') return `${providerId} · local changes not published`;
  return `${providerId} · ${syncStatusLabel(agent.sync_status).toLowerCase()}`;
}

function languageConfigurationLabel(agent: VoiceAgent) {
  const count = new Set([agent.language, ...agent.supported_languages]).size;
  if (count === 1) return 'Single-language configuration';
  if (agent.language_switching_enabled && agent.language_switching_mode === 'automatic') {
    return `${count} languages · automatic switching configured`;
  }
  return `${count} languages · automatic switching off`;
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}
