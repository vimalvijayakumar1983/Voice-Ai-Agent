import { FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import {
  BadgeCheck,
  BookOpenCheck,
  Bot,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  CloudUpload,
  FileText,
  Globe2,
  Layers3,
  Link2,
  Loader2,
  Plus,
  Pencil,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Trash2,
  Unlink,
  Upload,
  X,
} from 'lucide-react';
import Layout from '@/components/Layout';
import {
  api,
  CurrentUser,
  KnowledgeBase,
  KnowledgeScope,
  KnowledgeSource,
  VoiceAgent,
} from '@/lib/api';
import styles from '@/styles/Knowledge.module.css';

type Notice = { type: 'success' | 'error' | 'info'; text: string };
type SourceMode = 'urls' | 'sitemap' | 'pdf' | 'text';
type KnowledgeActionOptions = {
  syncAgentIds?: string[];
  syncPendingBindings?: boolean;
};

const AGENT_SYNC_POLL_MS = 1500;
const AGENT_SYNC_MAX_ATTEMPTS = 40;

const scopeOptions: Array<{ value: KnowledgeScope; label: string }> = [
  { value: 'workspace', label: 'Whole workspace' },
  { value: 'group', label: 'Group' },
  { value: 'division', label: 'Division' },
  { value: 'branch', label: 'Branch' },
  { value: 'department', label: 'Department' },
];

export default function KnowledgeStudio() {
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [query, setQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const [showCreate, setShowCreate] = useState(false);
  const [showEdit, setShowEdit] = useState(false);
  const [sourceMode, setSourceMode] = useState<SourceMode>('urls');
  const [sitemapUrls, setSitemapUrls] = useState<string[]>([]);
  const [selectedSitemapUrls, setSelectedSitemapUrls] = useState<Set<string>>(new Set());
  const detailHeadingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    let active = true;
    Promise.all([api.listKnowledgeBases(), api.listAgents(), api.getMe()])
      .then(([bases, loadedAgents, loadedUser]) => {
        if (!active) return;
        setKnowledgeBases(bases);
        setAgents(loadedAgents);
        setCurrentUser(loadedUser);
        setSelectedId((current) => current || bases[0]?.id || null);
      })
      .catch((error) => {
        if (active) setNotice({ type: 'error', text: errorMessage(error, 'Could not load Knowledge Studio.') });
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, []);

  const selected = knowledgeBases.find((kb) => kb.id === selectedId) || null;
  const canEditKnowledge = Boolean(currentUser && currentUser.role !== 'viewer');
  const canGovernKnowledge = currentUser?.role === 'owner' || currentUser?.role === 'admin';
  const filtered = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return knowledgeBases.filter((kb) => {
      const matchesQuery = !normalized || [kb.name, kb.description, kb.scope_label, ...kb.tags]
        .some((value) => value?.toLowerCase().includes(normalized));
      const matchesStatus = statusFilter === 'all'
        || (statusFilter === 'approved' ? kb.approval_status === 'approved' : kb.sync_status === statusFilter);
      return matchesQuery && matchesStatus;
    });
  }, [knowledgeBases, query, statusFilter]);

  const indexed = knowledgeBases.reduce((total, kb) => total + kb.indexed_source_count, 0);
  const processing = knowledgeBases.filter((kb) => kb.sync_status === 'processing' || kb.sync_status === 'provisioning').length;
  const boundAgents = new Set(knowledgeBases.flatMap((kb) => kb.agent_bindings.map((binding) => binding.agent_id))).size;

  const replaceKnowledgeBase = (updated: KnowledgeBase) => {
    setKnowledgeBases((current) => current.map((kb) => kb.id === updated.id ? updated : kb));
  };

  const replaceAgent = (updated: VoiceAgent) => {
    setAgents((current) => current.map((agent) => agent.id === updated.id ? updated : agent));
  };

  const syncAgentUntilSettled = async (agentId: string) => {
    let latest = await api.getAgent(agentId);
    replaceAgent(latest);
    if (!latest.provider_agent_id) return latest;

    for (let attempt = 0; attempt < AGENT_SYNC_MAX_ATTEMPTS; attempt += 1) {
      if (latest.sync_status === 'synced') return latest;
      try {
        latest = await api.syncSmallestAgent(agentId);
        replaceAgent(latest);
      } catch (error) {
        const message = errorMessage(error, 'Provider synchronization failed.');
        if (!message.toLowerCase().includes('still in progress')) throw error;
      }
      if (latest.sync_status === 'synced') return latest;
      if (latest.sync_status === 'error') {
        throw new Error(`${latest.name} could not publish its knowledge-base revision.`);
      }
      await new Promise((resolve) => window.setTimeout(resolve, AGENT_SYNC_POLL_MS));
      latest = await api.getAgent(agentId);
      replaceAgent(latest);
    }
    throw new Error(`${latest.name} is still being reviewed by Smallest.ai. Check status shortly.`);
  };

  const synchronizeAgents = async (agentIds: string[]) => {
    const uniqueAgentIds = Array.from(new Set(agentIds));
    if (!uniqueAgentIds.length) return 0;
    for (const agentId of uniqueAgentIds) await syncAgentUntilSettled(agentId);
    return uniqueAgentIds.length;
  };

  const runAction = async (
    key: string,
    action: () => Promise<KnowledgeBase>,
    success: string,
    options: KnowledgeActionOptions = {},
  ) => {
    setWorking(key);
    setNotice(null);
    let updated: KnowledgeBase;
    try {
      updated = await action();
      replaceKnowledgeBase(updated);
    } catch (error) {
      setNotice({ type: 'error', text: errorMessage(error, 'The knowledge operation failed.') });
      setWorking(null);
      return;
    }

    const pendingAgentIds = options.syncAgentIds
      || (options.syncPendingBindings && updated.approval_status === 'approved'
        ? updated.agent_bindings
          .filter((binding) => binding.sync_status !== 'synced')
          .map((binding) => binding.agent_id)
        : []);
    if (!pendingAgentIds.length) {
      setNotice({ type: 'success', text: success });
      setWorking(null);
      return;
    }

    setNotice({
      type: 'info',
      text: `${success} Publishing the updated knowledge tool to ${pendingAgentIds.length} bound agent${pendingAgentIds.length === 1 ? '' : 's'}…`,
    });
    try {
      const syncedCount = await synchronizeAgents(pendingAgentIds);
      const refreshed = await api.getKnowledgeBase(updated.id);
      replaceKnowledgeBase(refreshed);
      setNotice({
        type: 'success',
        text: `${success} ${syncedCount} bound agent${syncedCount === 1 ? ' is' : 's are'} published and verified.`,
      });
    } catch (error) {
      setNotice({
        type: 'error',
        text: `${success} Automatic agent synchronization did not complete: ${errorMessage(error, 'Check the agent provider status.')}`,
      });
    } finally {
      setWorking(null);
    }
  };

  const createKnowledgeBase = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setWorking('create');
    setNotice(null);
    try {
      const created = await api.createKnowledgeBase({
        name: String(form.get('name') || ''),
        description: String(form.get('description') || ''),
        scope_type: String(form.get('scope_type') || 'workspace') as KnowledgeScope,
        scope_label: String(form.get('scope_label') || ''),
        languages: String(form.get('languages') || 'en').split(',').map((item) => item.trim()).filter(Boolean),
        tags: String(form.get('tags') || '').split(',').map((item) => item.trim()).filter(Boolean),
      });
      setKnowledgeBases((current) => [created, ...current]);
      setSelectedId(created.id);
      setShowCreate(false);
      setNotice({ type: 'success', text: `${created.name} was created as a governed local draft.` });
      window.requestAnimationFrame(() => detailHeadingRef.current?.focus());
    } catch (error) {
      setNotice({ type: 'error', text: errorMessage(error, 'Could not create the knowledge base.') });
    } finally {
      setWorking(null);
    }
  };

  const updateKnowledgeBase = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selected) return;
    const form = new FormData(event.currentTarget);
    setWorking('update');
    setNotice(null);
    try {
      const updated = await api.updateKnowledgeBase(selected.id, {
        name: String(form.get('name') || ''),
        description: String(form.get('description') || ''),
        scope_type: String(form.get('scope_type') || 'workspace') as KnowledgeScope,
        scope_label: String(form.get('scope_label') || ''),
        languages: String(form.get('languages') || 'en').split(',').map((item) => item.trim()).filter(Boolean),
        tags: String(form.get('tags') || '').split(',').map((item) => item.trim()).filter(Boolean),
      });
      replaceKnowledgeBase(updated);
      setShowEdit(false);
      setNotice({ type: 'success', text: `${updated.name} details were updated.` });
      window.requestAnimationFrame(() => detailHeadingRef.current?.focus());
    } catch (error) {
      setNotice({ type: 'error', text: errorMessage(error, 'Could not update the knowledge base.') });
    } finally {
      setWorking(null);
    }
  };

  const addUrls = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selected) return;
    const form = new FormData(event.currentTarget);
    const urls = String(form.get('urls') || '').split(/[\n,]+/).map((url) => url.trim()).filter(Boolean);
    await runAction(
      'add-urls',
      () => api.addKnowledgeUrls(selected.id, urls),
      `${urls.length} website source${urls.length === 1 ? '' : 's'} queued for indexing.`,
      { syncPendingBindings: true },
    );
    event.currentTarget.reset();
  };

  const discoverSitemap = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selected) return;
    const sitemapUrl = String(new FormData(event.currentTarget).get('sitemap_url') || '');
    setWorking('discover-sitemap');
    setNotice(null);
    try {
      const result = await api.discoverKnowledgeSitemap(selected.id, sitemapUrl);
      setSitemapUrls(result.urls);
      setSelectedSitemapUrls(new Set(result.urls));
      setNotice({ type: 'info', text: `${result.urls.length} URLs discovered. Review the selection before indexing.` });
    } catch (error) {
      setNotice({ type: 'error', text: errorMessage(error, 'Could not read the sitemap.') });
    } finally {
      setWorking(null);
    }
  };

  const indexSitemapSelection = async () => {
    if (!selected || !selectedSitemapUrls.size) return;
    await runAction(
      'index-sitemap',
      () => api.addKnowledgeUrls(selected.id, Array.from(selectedSitemapUrls)),
      `${selectedSitemapUrls.size} curated pages queued for indexing.`,
      { syncPendingBindings: true },
    );
    setSitemapUrls([]);
    setSelectedSitemapUrls(new Set());
  };

  const uploadPdf = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selected) return;
    const input = event.currentTarget.elements.namedItem('media') as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    await runAction(
      'upload-pdf',
      () => api.uploadKnowledgePdf(selected.id, file),
      `${file.name} was queued for indexing.`,
      { syncPendingBindings: true },
    );
    event.currentTarget.reset();
  };

  const addText = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selected) return;
    const form = new FormData(event.currentTarget);
    await runAction(
      'add-text',
      () => api.addKnowledgeText(selected.id, String(form.get('text_name') || ''), String(form.get('text_content') || '')),
      'The text source was saved locally. Provider indexing will remain clearly marked unavailable.',
    );
    event.currentTarget.reset();
  };

  const deleteSelected = async () => {
    if (!selected || !window.confirm(`Delete ${selected.name}? This permanently removes its provider copy and sources.`)) return;
    setWorking('delete');
    setNotice(null);
    try {
      await api.deleteKnowledgeBase(selected.id);
      const remaining = knowledgeBases.filter((kb) => kb.id !== selected.id);
      setKnowledgeBases(remaining);
      setSelectedId(remaining[0]?.id || null);
      setNotice({ type: 'success', text: `${selected.name} was deleted.` });
    } catch (error) {
      setNotice({ type: 'error', text: errorMessage(error, 'Could not delete the knowledge base.') });
    } finally {
      setWorking(null);
    }
  };

  return (
    <Layout>
      <div className="page-header">
        <div>
          <span className="page-kicker">Ground & govern</span>
          <h1>Knowledge Studio</h1>
          <p className="page-subtitle">Turn approved business content into traceable, provider-ready knowledge for every voice agent.</p>
        </div>
        <div className="header-actions">
          <button type="button" className="btn btn-secondary" disabled={!selected || working !== null || !canEditKnowledge} onClick={() => selected && runAction('refresh', () => api.refreshKnowledgeBase(selected.id), 'Provider processing status refreshed.', { syncPendingBindings: true })}>
            <RefreshCw size={14} className={working === 'refresh' ? 'spin' : undefined} /> Refresh status
          </button>
          {canEditKnowledge && <button type="button" className="btn btn-primary" onClick={() => { setShowEdit(false); setShowCreate((open) => !open); }}>
            {showCreate ? <X size={14} /> : <Plus size={14} />}{showCreate ? 'Close' : 'New knowledge base'}
          </button>}
        </div>
      </div>

      {notice && (
        <div className={`provider-alert ${notice.type === 'error' ? 'provider-alert-error' : ''}`} role={notice.type === 'error' ? 'alert' : 'status'} aria-live={notice.type === 'error' ? 'assertive' : 'polite'}>
          {notice.type === 'error' ? <CircleAlert size={15} /> : notice.type === 'success' ? <CheckCircle2 size={15} /> : <Sparkles size={15} />}
          <span>{notice.text}</span>
        </div>
      )}

      {currentUser?.role === 'viewer' && <div className="provider-alert" role="status"><ShieldCheck size={15} /><span>Viewer access is read-only. Ask an owner or administrator to change sources, approval, or agent access.</span></div>}

      {showCreate && <KnowledgeFormPanel mode="create" busy={working === 'create'} onSubmit={createKnowledgeBase} onCancel={() => setShowCreate(false)} />}
      {showEdit && selected && <KnowledgeFormPanel key={selected.id} mode="edit" knowledge={selected} busy={working === 'update'} onSubmit={updateKnowledgeBase} onCancel={() => setShowEdit(false)} />}

      <section className={styles.stats} aria-label="Knowledge health">
        <Metric icon={BookOpenCheck} label="Knowledge bases" value={knowledgeBases.length} detail={`${knowledgeBases.filter((kb) => kb.approval_status === 'approved').length} approved`} />
        <Metric icon={BadgeCheck} label="Indexed sources" value={indexed} detail="Provider-confirmed" tone="success" />
        <Metric icon={RefreshCw} label="Processing" value={processing} detail="Awaiting provider" tone={processing ? 'warning' : 'neutral'} />
        <Metric icon={Bot} label="Bound agents" value={boundAgents} detail={`${agents.length - boundAgents} available`} />
      </section>

      {loading ? (
        <div className="page-loading" role="status"><Loader2 className="spin" size={17} /> Loading governed knowledge…</div>
      ) : knowledgeBases.length === 0 ? (
        <section className={styles.welcomeEmpty}>
          <div className={styles.emptyIcon}><Layers3 size={26} /></div>
          <span className="page-kicker">Knowledge starts with trusted sources</span>
          <h2>Give every agent the right answer—not a larger prompt.</h2>
          <p>Create a reusable knowledge base, select only approved website pages or documents, verify provider indexing, then bind it to an agent.</p>
          <div className={styles.emptySteps}>
            <span><strong>1</strong>Create and scope</span><ChevronRight size={15} />
            <span><strong>2</strong>Add trusted sources</span><ChevronRight size={15} />
            <span><strong>3</strong>Approve and bind</span>
          </div>
          {canEditKnowledge && <button type="button" className="btn btn-primary" onClick={() => setShowCreate(true)}><Plus size={14} /> Create first knowledge base</button>}
        </section>
      ) : (
        <div className={styles.workspace}>
          <aside className={styles.library} aria-label="Knowledge library">
            <div className={styles.libraryHeader}>
              <div><span className="page-kicker">Library</span><h2>Knowledge bases</h2></div>
              <span className="badge badge-neutral">{filtered.length}</span>
            </div>
            <div className={styles.filters}>
              <label className={styles.searchField} htmlFor="knowledge-search"><Search size={14} /><span className="sr-only">Search knowledge</span><input id="knowledge-search" type="search" value={query} placeholder="Search name, scope or tag" onChange={(event) => setQuery(event.target.value)} /></label>
              <select aria-label="Filter knowledge status" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
                <option value="all">All statuses</option>
                <option value="ready">Ready</option>
                <option value="processing">Processing</option>
                <option value="error">Needs attention</option>
                <option value="approved">Approved</option>
              </select>
            </div>
            <div className={styles.libraryList}>
              {filtered.map((kb) => (
                <button type="button" key={kb.id} className={`${styles.libraryItem} ${selectedId === kb.id ? styles.libraryItemSelected : ''}`} aria-pressed={selectedId === kb.id} onClick={() => { setSelectedId(kb.id); setSitemapUrls([]); setShowEdit(false); }}>
                  <span className={styles.libraryItemIcon}><BookOpenCheck size={16} /></span>
                  <span className={styles.libraryItemBody}><strong>{kb.name}</strong><small>{scopeLabel(kb)} · {kb.source_count} sources</small></span>
                  <StatusDot status={kb.sync_status} />
                </button>
              ))}
              {filtered.length === 0 && <div className={styles.noResults}>No knowledge bases match these filters.</div>}
            </div>
          </aside>

          {selected && (
            <div className={styles.detail} aria-labelledby="knowledge-detail-heading">
              <section className={styles.detailHero}>
                <div className={styles.detailIdentity}>
                  <div className={styles.heroIcon}><BookOpenCheck size={23} /></div>
                  <div>
                    <div className={styles.eyebrowRow}><span className="page-kicker">{scopeLabel(selected)}</span><StatusBadge status={selected.sync_status} /><span className={`badge ${selected.approval_status === 'approved' ? 'badge-success' : 'badge-neutral'}`}>{selected.approval_status}</span></div>
                    <h2 ref={detailHeadingRef} id="knowledge-detail-heading" tabIndex={-1}>{selected.name}</h2>
                    <p>{selected.description || 'No description added yet.'}</p>
                  </div>
                </div>
                <div className={styles.heroActions}>
                  {canEditKnowledge && !selected.provider_knowledge_base_id && <button type="button" className="btn btn-secondary btn-sm" disabled={working !== null} onClick={() => runAction('provision', () => api.provisionKnowledgeBase(selected.id), 'A secure provider knowledge base was created.')}><CloudUpload size={12} /> Connect provider</button>}
                  {canEditKnowledge && <button type="button" className="btn btn-secondary btn-sm" disabled={working !== null} onClick={() => { setShowCreate(false); setShowEdit(true); }}><Pencil size={12} /> Edit details</button>}
                  {canGovernKnowledge && <button type="button" className="btn btn-ghost btn-sm" disabled={working !== null} onClick={deleteSelected} aria-label={`Delete ${selected.name}`}><Trash2 size={13} /> Delete</button>}
                </div>
                <div className={styles.progressRail} aria-label={`${selected.indexed_source_count} of ${selected.source_count} sources indexed`}>
                  <div style={{ width: `${selected.source_count ? Math.round(selected.indexed_source_count / selected.source_count * 100) : 0}%` }} />
                </div>
                <div className={styles.heroMeta}>
                  <span><strong>{selected.indexed_source_count}/{selected.source_count}</strong> indexed</span>
                  <span><strong>{selected.languages.join(', ').toUpperCase()}</strong> languages</span>
                  <span><strong>{selected.agent_bindings.length}</strong> bound agents</span>
                  <span><strong>{selected.last_synced_at ? formatDate(selected.last_synced_at) : 'Never'}</strong> provider check</span>
                </div>
                {selected.sync_error && <div className={styles.inlineError} role="alert"><CircleAlert size={14} /><span>{selected.sync_error}</span></div>}
              </section>

              {canEditKnowledge && <section className={styles.section} aria-labelledby="source-builder-heading">
                <div className={styles.sectionHeading}>
                  <div><span className={styles.sectionIcon}><Plus size={15} /></span><div><h3 id="source-builder-heading">Add trusted knowledge</h3><p>Provider credentials stay on the server. Website content is curated before indexing.</p></div></div>
                </div>
                <div className={styles.sourceTabs} role="tablist" aria-label="Knowledge source type">
                  <SourceTab active={sourceMode === 'urls'} icon={Globe2} label="Web pages" onClick={() => setSourceMode('urls')} />
                  <SourceTab active={sourceMode === 'sitemap'} icon={Link2} label="Sitemap" onClick={() => setSourceMode('sitemap')} />
                  <SourceTab active={sourceMode === 'pdf'} icon={FileText} label="PDF" onClick={() => setSourceMode('pdf')} />
                  <SourceTab active={sourceMode === 'text'} icon={Layers3} label="Text" onClick={() => setSourceMode('text')} />
                </div>
                <div className={styles.sourceBuilder} role="tabpanel">
                  {sourceMode === 'urls' && <UrlForm busy={working === 'add-urls'} onSubmit={addUrls} />}
                  {sourceMode === 'sitemap' && <SitemapForm busy={working === 'discover-sitemap' || working === 'index-sitemap'} onSubmit={discoverSitemap} urls={sitemapUrls} selected={selectedSitemapUrls} onToggle={(url) => setSelectedSitemapUrls((current) => { const next = new Set(current); if (next.has(url)) next.delete(url); else next.add(url); return next; })} onToggleAll={() => setSelectedSitemapUrls((current) => current.size === sitemapUrls.length ? new Set() : new Set(sitemapUrls))} onIndex={indexSitemapSelection} />}
                  {sourceMode === 'pdf' && <PdfForm busy={working === 'upload-pdf'} onSubmit={uploadPdf} />}
                  {sourceMode === 'text' && <TextForm busy={working === 'add-text'} onSubmit={addText} />}
                </div>
              </section>}

              <SourcesSection sources={selected.sources} />

              <section className={styles.section} aria-labelledby="governance-heading">
                <div className={styles.sectionHeading}>
                  <div><span className={styles.sectionIcon}><ShieldCheck size={15} /></span><div><h3 id="governance-heading">Approval & agent access</h3><p>Only provider-indexed, approved knowledge can be published to a live agent.</p></div></div>
                </div>
                <div className={styles.governanceGrid}>
                  <div className={styles.approvalCard}>
                    <span className={styles.miniLabel}>Release gate</span>
                    <strong>{selected.approval_status === 'approved' ? 'Approved for agent use' : 'Draft—not available to agents'}</strong>
                    <p>{selected.sync_status === 'ready' ? 'Provider indexing is complete. An owner or admin may change approval.' : 'Complete provider indexing before approval becomes available.'}</p>
                    {canGovernKnowledge && <button type="button" className={`btn ${selected.approval_status === 'approved' ? 'btn-secondary' : 'btn-primary'} btn-sm`} disabled={working !== null || (selected.approval_status !== 'approved' && selected.sync_status !== 'ready')} onClick={() => runAction('approve', () => api.approveKnowledgeBase(selected.id, selected.approval_status !== 'approved'), selected.approval_status === 'approved' ? 'Approval removed; bound agents require review.' : 'Knowledge approved for agent binding.')}>
                      {selected.approval_status === 'approved' ? <X size={12} /> : <Check size={12} />}{selected.approval_status === 'approved' ? 'Return to draft' : 'Approve knowledge'}
                    </button>}
                  </div>
                  <AgentBinding selected={selected} agents={agents} busy={working !== null} canManage={canGovernKnowledge} onBind={(agentId) => runAction('bind', () => api.bindKnowledgeAgent(selected.id, agentId), 'Agent binding saved.', { syncAgentIds: [agentId] })} onUnbind={(agentId) => runAction('unbind', () => api.unbindKnowledgeAgent(selected.id, agentId), 'Agent was unbound.', { syncAgentIds: [agentId] })} />
                </div>
              </section>
            </div>
          )}
        </div>
      )}
    </Layout>
  );
}

function KnowledgeFormPanel({ mode, knowledge, busy, onSubmit, onCancel }: { mode: 'create' | 'edit'; knowledge?: KnowledgeBase; busy: boolean; onSubmit: (event: FormEvent<HTMLFormElement>) => void; onCancel: () => void }) {
  const editing = mode === 'edit';
  return <section className={styles.createPanel} aria-labelledby="create-knowledge-heading">
    <div className={styles.createAside}><Sparkles size={21} /><span className="page-kicker">{editing ? 'Keep context accurate' : 'Govern from day one'}</span><h2 id="create-knowledge-heading">{editing ? 'Edit knowledge details' : 'Create a reusable knowledge base'}</h2><p>{editing ? 'Update business scope, languages, and discovery metadata without losing source provenance or bindings.' : 'Scope it to the correct business level. Sources and agent access are added after creation.'}</p><div><span><ShieldCheck size={14} /> Tenant isolated</span><span><Layers3 size={14} /> Provider neutral</span><span><BadgeCheck size={14} /> Approval gated</span></div></div>
    <form className={styles.createForm} onSubmit={onSubmit}>
      <div className="form-grid">
        <div className="form-group"><label htmlFor="kb-name">Name <span>Required</span></label><input id="kb-name" name="name" maxLength={40} required defaultValue={knowledge?.name || ''} placeholder="FEPY product & policy knowledge" /></div>
        <div className="form-group"><label htmlFor="kb-scope">Business scope</label><select id="kb-scope" name="scope_type" defaultValue={knowledge?.scope_type || 'workspace'}>{scopeOptions.map((scope) => <option key={scope.value} value={scope.value}>{scope.label}</option>)}</select></div>
      </div>
      <div className="form-group"><label htmlFor="kb-description">Purpose</label><textarea id="kb-description" name="description" maxLength={1000} defaultValue={knowledge?.description || ''} placeholder="Approved product descriptions, delivery policies, returns and customer FAQs." /></div>
      <div className="form-grid">
        <div className="form-group"><label htmlFor="kb-scope-label">Scope name</label><input id="kb-scope-label" name="scope_label" maxLength={255} defaultValue={knowledge?.scope_label || ''} placeholder="E-commerce division" /></div>
        <div className="form-group"><label htmlFor="kb-languages">Languages <span>Comma separated</span></label><input id="kb-languages" name="languages" defaultValue={knowledge?.languages.join(', ') || 'en'} placeholder="en, ar, hi, ml" /></div>
      </div>
      <div className="form-group"><label htmlFor="kb-tags">Tags <span>Comma separated</span></label><input id="kb-tags" name="tags" defaultValue={knowledge?.tags.join(', ') || ''} placeholder="products, delivery, returns" /></div>
      <div className={styles.formActions}><button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button><button type="submit" className="btn btn-primary" disabled={busy}>{busy ? <Loader2 className="spin" size={14} /> : editing ? <Check size={14} /> : <Plus size={14} />} {editing ? 'Save details' : 'Create draft'}</button></div>
    </form>
  </section>;
}

function Metric({ icon: Icon, label, value, detail, tone = 'neutral' }: { icon: typeof BookOpenCheck; label: string; value: number; detail: string; tone?: string }) {
  return <article className={`${styles.metric} ${styles[`metric_${tone}`] || ''}`}><span><Icon size={17} /></span><div><small>{label}</small><strong>{value}</strong><p>{detail}</p></div></article>;
}

function SourceTab({ active, icon: Icon, label, onClick }: { active: boolean; icon: typeof Globe2; label: string; onClick: () => void }) {
  return <button type="button" role="tab" aria-selected={active} className={active ? styles.sourceTabActive : ''} onClick={onClick}><Icon size={14} /> {label}</button>;
}

function UrlForm({ busy, onSubmit }: { busy: boolean; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) {
  return <form onSubmit={onSubmit} className={styles.builderForm}><div><label htmlFor="source-urls">Approved page URLs</label><p>Enter one public HTTPS page per line. Add policy, product and FAQ pages—not account or checkout pages.</p></div><textarea id="source-urls" name="urls" required placeholder={'https://www.example.com/products\nhttps://www.example.com/delivery-policy'} /><button type="submit" className="btn btn-primary" disabled={busy}>{busy ? <Loader2 className="spin" size={14} /> : <CloudUpload size={14} />} Index pages</button></form>;
}

function SitemapForm({ busy, onSubmit, urls, selected, onToggle, onToggleAll, onIndex }: { busy: boolean; onSubmit: (event: FormEvent<HTMLFormElement>) => void; urls: string[]; selected: Set<string>; onToggle: (url: string) => void; onToggleAll: () => void; onIndex: () => void }) {
  return <div className={styles.sitemapBuilder}><form onSubmit={onSubmit} className={styles.inlineForm}><div><label htmlFor="sitemap-url">Sitemap URL</label><p>We discover pages first. You decide exactly what becomes searchable.</p></div><div><input id="sitemap-url" name="sitemap_url" type="url" required placeholder="https://www.example.com/sitemap.xml" /><button type="submit" className="btn btn-secondary" disabled={busy}>{busy ? <Loader2 className="spin" size={14} /> : <Search size={14} />} Discover</button></div></form>{urls.length > 0 && <div className={styles.urlPicker}><div><label><input type="checkbox" checked={selected.size === urls.length} onChange={onToggleAll} /> Select all discovered pages</label><span>{selected.size} of {urls.length} selected</span></div><ul>{urls.slice(0, 100).map((url) => <li key={url}><label><input type="checkbox" checked={selected.has(url)} onChange={() => onToggle(url)} /><span>{url}</span></label></li>)}</ul>{urls.length > 100 && <p>Showing the first 100 URLs. Narrow the sitemap before indexing a larger set.</p>}<button type="button" className="btn btn-primary" disabled={busy || !selected.size} onClick={onIndex}><Check size={14} /> Index {selected.size} selected</button></div>}</div>;
}

function PdfForm({ busy, onSubmit }: { busy: boolean; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) {
  return <form onSubmit={onSubmit} className={styles.uploadForm}><div className={styles.uploadDrop}><Upload size={22} /><div><label htmlFor="knowledge-pdf">Choose an approved PDF</label><p>PDF only · maximum 8 MB · remove confidential or customer-specific data first</p></div><input id="knowledge-pdf" name="media" type="file" accept="application/pdf,.pdf" required /></div><button type="submit" className="btn btn-primary" disabled={busy}>{busy ? <Loader2 className="spin" size={14} /> : <CloudUpload size={14} />} Upload and index</button></form>;
}

function TextForm({ busy, onSubmit }: { busy: boolean; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) {
  return <form onSubmit={onSubmit} className={styles.builderForm}><div><label htmlFor="text-name">Internal text note</label><p>Saved with clear “local only” status until the selected provider supports text ingestion.</p></div><input id="text-name" name="text_name" required maxLength={255} placeholder="Approved returns FAQ" /><textarea id="text-content" name="text_content" required minLength={20} maxLength={100000} placeholder="Paste approved question-and-answer content here…" /><button type="submit" className="btn btn-secondary" disabled={busy}>{busy ? <Loader2 className="spin" size={14} /> : <Plus size={14} />} Save local source</button></form>;
}

function SourcesSection({ sources }: { sources: KnowledgeSource[] }) {
  return <section className={styles.section} aria-labelledby="sources-heading"><div className={styles.sectionHeading}><div><span className={styles.sectionIcon}><Layers3 size={15} /></span><div><h3 id="sources-heading">Source inventory</h3><p>Provider status is authoritative. “Processing” content is not yet available to callers.</p></div></div><span className="badge badge-neutral">{sources.length} sources</span></div>{sources.length === 0 ? <div className={styles.sourceEmpty}><FileText size={20} /><div><strong>No sources yet</strong><p>Add curated web pages or an approved PDF to begin indexing.</p></div></div> : <div className={styles.sourceList}>{sources.map((source) => <article className={styles.sourceRow} key={source.id}><span className={styles.sourceTypeIcon}>{source.source_type === 'file' ? <FileText size={16} /> : source.source_type === 'text' ? <Layers3 size={16} /> : <Globe2 size={16} />}</span><div className={styles.sourceIdentity}><strong>{source.name}</strong><span>{source.location || (source.size_bytes ? formatBytes(source.size_bytes) : source.source_type)}</span>{source.error_message && <p>{source.error_message}</p>}</div><span className={`badge ${sourceBadge(source.status)}`}>{source.status.replace('_', ' ')}</span><time>{formatDate(source.last_synced_at || source.updated_at)}</time></article>)}</div>}</section>;
}

function AgentBinding({ selected, agents, busy, canManage, onBind, onUnbind }: { selected: KnowledgeBase; agents: VoiceAgent[]; busy: boolean; canManage: boolean; onBind: (agentId: string) => void; onUnbind: (agentId: string) => void }) {
  const [agentId, setAgentId] = useState('');
  const boundIds = new Set(selected.agent_bindings.map((binding) => binding.agent_id));
  const available = agents.filter((agent) => !boundIds.has(agent.id));
  return <div className={styles.bindingCard}><div className={styles.bindingHeader}><div><span className={styles.miniLabel}>Agent access</span><strong>{selected.agent_bindings.length} agents bound</strong></div><Bot size={18} /></div><div className={styles.bindingList}>{selected.agent_bindings.map((binding) => <div key={binding.id}><span className={styles.agentAvatar}><Bot size={13} /></span><span><strong>{binding.agent_name}</strong><small>{binding.sync_status === 'synced' ? 'Live provider binding' : 'Publish agent to make binding live'}</small></span>{canManage && <button type="button" className="icon-button" disabled={busy} onClick={() => onUnbind(binding.agent_id)} aria-label={`Unbind ${binding.agent_name}`}><Unlink size={14} /></button>}</div>)}{selected.agent_bindings.length === 0 && <p className={styles.bindingEmpty}>No agents can use this knowledge yet.</p>}</div>{canManage && <div className={styles.bindControl}><select aria-label="Agent to bind" value={agentId} disabled={busy || selected.approval_status !== 'approved'} onChange={(event) => setAgentId(event.target.value)}><option value="">Select an agent…</option>{available.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select><button type="button" className="btn btn-secondary btn-sm" disabled={busy || !agentId || selected.approval_status !== 'approved'} onClick={() => { onBind(agentId); setAgentId(''); }}><Link2 size={12} /> Bind</button></div>}</div>;
}

function StatusBadge({ status }: { status: KnowledgeBase['sync_status'] }) {
  const labels: Record<KnowledgeBase['sync_status'], string> = { local_only: 'Local draft', provisioning: 'Connecting', processing: 'Processing', ready: 'Ready', error: 'Needs attention' };
  return <span className={`badge ${status === 'ready' ? 'badge-success' : status === 'error' ? 'badge-danger' : status === 'processing' || status === 'provisioning' ? 'badge-warning' : 'badge-neutral'}`}>{labels[status]}</span>;
}

function StatusDot({ status }: { status: KnowledgeBase['sync_status'] }) { return <span className={`${styles.statusDot} ${styles[`status_${status}`]}`} title={status.replace('_', ' ')} />; }
function scopeLabel(kb: KnowledgeBase) { return kb.scope_label || scopeOptions.find((scope) => scope.value === kb.scope_type)?.label || kb.scope_type; }
function sourceBadge(status: KnowledgeSource['status']) { if (status === 'indexed') return 'badge-success'; if (status === 'failed') return 'badge-danger'; if (status === 'processing' || status === 'pending') return 'badge-warning'; return 'badge-neutral'; }
function formatBytes(bytes: number) { return bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`; }
function formatDate(value: string) { return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)); }
function errorMessage(error: unknown, fallback: string) { return error instanceof Error ? error.message : fallback; }
