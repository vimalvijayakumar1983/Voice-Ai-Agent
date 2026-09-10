import { useRouter } from 'next/router';
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
  RotateCw,
  Search,
  ShieldCheck,
  Sparkles,
  Trash2,
  Unlink,
  Upload,
  X,
} from 'lucide-react';
import Layout from '@/components/Layout';
import KnowledgeAIWizard from '@/components/KnowledgeAIWizard';
import {
  api,
  CurrentUser,
  KnowledgeAIDraftRequest,
  KnowledgeAIDraftResponse,
  KnowledgeBase,
  KnowledgeCrawl,
  KnowledgeProcessingMode,
  KnowledgeServingRevision,
  KnowledgeScope,
  KnowledgeSource,
  VoiceAgent,
} from '@/lib/api';
import {
  canBindKnowledgeAgent,
  isVavNativeKnowledgeProvider,
  knowledgeBindingGuidance,
} from '@/lib/knowledge-binding.cjs';
import styles from '@/styles/Knowledge.module.css';

type Notice = { type: 'success' | 'error' | 'info'; text: string };
type SourceMode = 'crawl' | 'urls' | 'sitemap' | 'pdf' | 'text';

const SOURCE_REPAIR_POLL_MS = 2000;
const SOURCE_REPAIR_MAX_ATTEMPTS = 90;

const scopeOptions: Array<{ value: KnowledgeScope; label: string }> = [
  { value: 'workspace', label: 'Whole workspace' },
  { value: 'group', label: 'Group' },
  { value: 'division', label: 'Division' },
  { value: 'branch', label: 'Branch' },
  { value: 'department', label: 'Department' },
];

export default function KnowledgeStudio() {
  const router = useRouter();
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
  const [showAIWizard, setShowAIWizard] = useState(false);
  const [generatedDraft, setGeneratedDraft] = useState<KnowledgeAIDraftResponse | null>(null);
  const [showEdit, setShowEdit] = useState(false);
  const [sourceMode, setSourceMode] = useState<SourceMode>('crawl');
  const [sitemapUrls, setSitemapUrls] = useState<string[]>([]);
  const [selectedSitemapUrls, setSelectedSitemapUrls] = useState<Set<string>>(new Set());
  const [releaseHistory, setReleaseHistory] = useState<KnowledgeServingRevision[]>([]);
  const [releaseHistoryKnowledgeBaseId, setReleaseHistoryKnowledgeBaseId] = useState<string | null>(null);
  const [rollbackRevisionId, setRollbackRevisionId] = useState('');
  const [rollbackReason, setRollbackReason] = useState('');
  const detailHeadingRef = useRef<HTMLHeadingElement>(null);
  const selectedIdRef = useRef<string | null>(null);

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

  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  useEffect(() => {
    let active = true;
    if (!selectedId) return () => { active = false; };
    api.listKnowledgeReleases(selectedId)
      .then((releases) => {
        if (!active) return;
        setReleaseHistory(releases);
        setReleaseHistoryKnowledgeBaseId(selectedId);
        setRollbackRevisionId('');
        setRollbackReason('');
      })
      .catch((error) => {
        if (!active) return;
        setReleaseHistory([]);
        setReleaseHistoryKnowledgeBaseId(selectedId);
        setNotice({ type: 'error', text: errorMessage(error, 'Could not load release history.') });
      });
    return () => { active = false; };
  }, [selectedId]);

  const visibleReleaseHistory = releaseHistoryKnowledgeBaseId === selectedId ? releaseHistory : [];
  const rollbackCandidates = visibleReleaseHistory.filter(
    (release) => release.revision_id !== selected?.serving_revision?.revision_id,
  );
  const effectiveRollbackRevisionId = rollbackCandidates.some(
    (release) => release.revision_id === rollbackRevisionId,
  ) ? rollbackRevisionId : rollbackCandidates[0]?.revision_id || '';
  const selectedHasActiveRepair = Boolean(selected?.sources.some((source) => {
    const recovery = sourceRecovery(source);
    const compilation = sourceUploadCompilation(source);
    return recovery?.status === 'queued' || recovery?.status === 'processing'
      || compilation?.status === 'queued' || compilation?.status === 'processing';
  }) || selected?.crawls.some((crawl) => ['queued', 'discovering', 'indexing', 'retrying'].includes(crawl.status)));

  useEffect(() => {
    if (!selectedId || !selectedHasActiveRepair) return;
    let active = true;
    const timer = window.setInterval(() => {
      api.getKnowledgeBase(selectedId)
        .then((updated) => {
          if (!active) return;
          setKnowledgeBases((current) => current.map((kb) => kb.id === updated.id ? updated : kb));
        })
        .catch(() => undefined);
    }, SOURCE_REPAIR_POLL_MS);
    return () => { active = false; window.clearInterval(timer); };
  }, [selectedId, selectedHasActiveRepair]);

  const canEditKnowledge = Boolean(currentUser && currentUser.role !== 'viewer');
  const canGovernKnowledge = currentUser?.role === 'owner' || currentUser?.role === 'admin';
  const aiWizardVisible = showAIWizard || (router.query.create === 'ai' && !showCreate);
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
  const processing = knowledgeBases.filter((kb) => kb.sync_status === 'processing').length;
  const boundAgents = new Set(knowledgeBases.flatMap((kb) => kb.agent_bindings.map((binding) => binding.agent_id))).size;

  const replaceKnowledgeBase = useCallback((updated: KnowledgeBase) => {
    setKnowledgeBases((current) => current.map((kb) => kb.id === updated.id ? updated : kb));
  }, []);

  const runAction = async (
    key: string,
    action: () => Promise<KnowledgeBase>,
    success: string,
  ) => {
    setWorking(key);
    setNotice(null);
    let updated: KnowledgeBase;
    try {
      updated = await action();
      replaceKnowledgeBase(updated);
      if (key === 'approve' && updated.id === selectedIdRef.current) {
        try {
          const releases = await api.listKnowledgeReleases(updated.id);
          if (updated.id === selectedIdRef.current) {
            setReleaseHistory(releases);
            setReleaseHistoryKnowledgeBaseId(updated.id);
          }
        } catch {
          // Approval succeeded; the next selection or reload retries this
          // read-only history refresh without misreporting publication failure.
        }
      }
    } catch (error) {
      setNotice({ type: 'error', text: errorMessage(error, 'The knowledge operation failed.') });
      setWorking(null);
      return;
    }

    setNotice({ type: 'success', text: success });
    setWorking(null);
  };

  const reactivateRelease = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selected || !effectiveRollbackRevisionId || rollbackReason.trim().length < 3) return;
    const target = visibleReleaseHistory.find(
      (release) => release.revision_id === effectiveRollbackRevisionId,
    );
    if (!target) return;
    if (!window.confirm(`Reactivate release ${target.revision_id.slice(0, 12)} for new calls? Calls already running keep their existing release.`)) return;
    setWorking('reactivate-release');
    setNotice(null);
    try {
      const updated = await api.reactivateKnowledgeRelease(selected.id, target.revision_id, {
        expected_current_revision_id: selected.serving_revision?.revision_id || null,
        reason: rollbackReason.trim(),
      });
      replaceKnowledgeBase(updated);
      const releases = await api.listKnowledgeReleases(selected.id);
      setReleaseHistory(releases);
      setRollbackRevisionId(releases.find((release) => release.revision_id !== target.revision_id)?.revision_id || '');
      setRollbackReason('');
      setNotice({ type: 'success', text: 'The selected immutable release and its speech lexicon are live for new calls. Draft content still requires review.' });
    } catch (error) {
      setNotice({ type: 'error', text: errorMessage(error, 'The historical release could not be reactivated.') });
    } finally {
      setWorking(null);
    }
  };

  const clearCreatePanels = () => {
    setShowCreate(false);
    setShowAIWizard(false);
    setGeneratedDraft(null);
    if (router.query.create === 'ai') void router.replace('/knowledge', undefined, { shallow: true });
  };

  const openManualCreate = () => {
    setShowEdit(false);
    setShowAIWizard(false);
    setGeneratedDraft(null);
    setShowCreate(true);
    setNotice(null);
    if (router.query.create === 'ai') void router.replace('/knowledge', undefined, { shallow: true });
  };

  const openAIWizard = () => {
    setShowEdit(false);
    setShowCreate(false);
    setGeneratedDraft(null);
    setShowAIWizard(true);
    setNotice(null);
  };

  const generateKnowledgeDraft = async (request: KnowledgeAIDraftRequest) => {
    setWorking('knowledge-ai-draft');
    setNotice(null);
    try {
      const result = await api.generateKnowledgeDraft(request);
      setGeneratedDraft(result);
      setShowAIWizard(false);
      setShowCreate(true);
      setNotice({ type: 'info', text: 'OpenAI generated governed metadata only. Review every field before creating the knowledge draft.' });
      if (router.query.create === 'ai') void router.replace('/knowledge', undefined, { shallow: true });
    } catch (error) {
      setNotice({ type: 'error', text: errorMessage(error, 'OpenAI could not generate the knowledge draft.') });
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
        owner_company: String(form.get('owner_company') || '').trim() || null,
        languages: String(form.get('languages') || 'en').split(',').map((item) => item.trim()).filter(Boolean),
        tags: String(form.get('tags') || '').split(',').map((item) => item.trim()).filter(Boolean),
      });
      setKnowledgeBases((current) => [created, ...current]);
      setSelectedId(created.id);
      setShowCreate(false);
      setGeneratedDraft(null);
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
        owner_company: String(form.get('owner_company') || '').trim() || null,
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
    );
    setSitemapUrls([]);
    setSelectedSitemapUrls(new Set());
  };

  const uploadPdf = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selected) return;
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const input = formElement.elements.namedItem('media') as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    await runAction(
      'upload-pdf',
      () => api.uploadKnowledgePdf(selected.id, file, String(form.get('processing_mode') || 'automatic') as KnowledgeProcessingMode),
      `${file.name} was extracted, compiled and indexed for review. Check any compiler warnings and approve the draft before agents use it.`,
    );
  };

  const addText = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selected) return;
    const form = new FormData(event.currentTarget);
    await runAction(
      'add-text',
      () => api.addKnowledgeText(selected.id, String(form.get('text_name') || ''), String(form.get('text_content') || ''), String(form.get('processing_mode') || 'automatic') as KnowledgeProcessingMode),
      'Text saved. AI processing continues in the background; follow its status in Source inventory. Review the completed facts before approval.',
    );
  };

  const compileSource = async (source: KnowledgeSource) => {
    if (!selected || !window.confirm(`Structure ${source.name} with AI? This uses your configured OpenAI account and may incur extraction charges. The original and live approved release stay unchanged until you approve the draft.`)) return;
    await runAction(
      `compile-source-${source.id}`,
      () => api.compileKnowledgeSource(selected.id, source.id),
      'Processing queued in place; no duplicate document was created. Follow Source inventory for progress, then review before approval.',
    );
  };

  const removeSource = async (source: KnowledgeSource) => {
    if (!selected || !window.confirm(
      `Stage removal of ${source.name}? It will be removed from the editable source set now. If a VAV release is already live, new VAV calls keep using that approved release until you approve these changes or choose “Revoke live release now.” Historical releases remain for audit.`,
    )) return;
    await runAction(
      `delete-source-${source.id}`,
      () => api.deleteKnowledgeSource(selected.id, source.id),
      `${source.name} was removed from the draft. Approve the pending VAV release to make the removal live.`,
    );
  };

  const startSiteCrawl = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selected) return;
    const form = new FormData(event.currentTarget);
    await runAction(
      'crawl-site',
      () => api.startKnowledgeCrawl(selected.id, {
        homepage_url: String(form.get('homepage_url') || ''),
        max_pages: Number(form.get('max_pages') || 100),
        max_depth: Number(form.get('max_depth') || 3),
        include_subdomains: form.get('include_subdomains') === 'on',
        processing_mode: String(form.get('processing_mode') || 'automatic') as KnowledgeProcessingMode,
      }),
      'Whole-site discovery started. Progress and every failed page will remain visible here.',
    );
  };

  const retrySiteCrawl = async (crawl: KnowledgeCrawl) => {
    if (!selected) return;
    await runAction(
      `retry-crawl-${crawl.id}`,
      () => api.retryKnowledgeCrawl(selected.id, crawl.id),
      crawl.failed_count
        ? `${crawl.failed_count} failed page${crawl.failed_count === 1 ? '' : 's'} queued for repair.`
        : 'Website discovery queued for another attempt.',
    );
  };

  const repairSource = async (source: KnowledgeSource) => {
    if (!selected) return;
    const knowledgeBaseId = selected.id;
    setWorking(`repair-source-${source.id}`);
    setNotice({ type: 'info', text: 'VAV is queuing safe download and recovery for this page…' });
    try {
      let updated = await api.repairKnowledgeSource(knowledgeBaseId, source.id);
      replaceKnowledgeBase(updated);
      for (let attempt = 0; attempt < SOURCE_REPAIR_MAX_ATTEMPTS; attempt += 1) {
        const currentSource = updated.sources.find((item) => item.id === source.id);
        const recovery = currentSource ? sourceRecovery(currentSource) : null;
        if (currentSource && recovery?.status === 'completed') {
          setNotice({ type: coverageNoticeType(currentSource), text: coverageNotice(currentSource) });
          return;
        }
        if (recovery?.status === 'failed' || currentSource?.status === 'failed') {
          throw new Error(currentSource?.error_message || recovery?.message || 'Page recovery failed.');
        }
        setNotice({
          type: 'info',
          text: recovery?.message || `VAV is ${recoveryStageLabel(recovery?.stage)}…`,
        });
        await new Promise((resolve) => window.setTimeout(resolve, SOURCE_REPAIR_POLL_MS));
        updated = await api.getKnowledgeBase(knowledgeBaseId);
        replaceKnowledgeBase(updated);
      }
      throw new Error('The page is still being processed. VAV will continue in the background.');
    } catch (error) {
      setNotice({ type: 'error', text: errorMessage(error, 'VAV could not recover this website page.') });
    } finally {
      setWorking(null);
    }
  };

  const reindexSelected = async () => {
    if (!selected || !window.confirm(
      `Re-index every source in ${selected.name} with the current extraction pipeline? PDFs are re-read from the stored file, pasted text is re-split, and website pages are downloaded and compiled again. Each source will show its coverage when it finishes. The approved release stays live until you approve the re-indexed draft.`,
    )) return;
    await runAction(
      'reindex',
      () => api.reindexKnowledgeBase(selected.id),
      'Re-indexing started. Follow Source inventory for progress, then review coverage before approval.',
    );
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
          <button type="button" className="btn btn-secondary" disabled={!selected || working !== null || !canEditKnowledge} onClick={() => selected && runAction('refresh', () => api.refreshKnowledgeBase(selected.id), 'Source counts refreshed and duplicate pages merged.')}>
            <RefreshCw size={14} className={working === 'refresh' ? 'spin' : undefined} /> Refresh status
          </button>
          {canEditKnowledge && <button type="button" className="btn btn-secondary" disabled={!selected || working !== null || selectedHasActiveRepair || selected.source_count === 0} onClick={reindexSelected}>
            <RotateCw size={14} className={working === 'reindex' ? 'spin' : undefined} /> Re-index all sources
          </button>}
          {canEditKnowledge && <button type="button" className="btn btn-secondary" disabled={working !== null} onClick={aiWizardVisible ? clearCreatePanels : openAIWizard}>
            {aiWizardVisible ? <X size={14} /> : <Sparkles size={14} />}{aiWizardVisible ? 'Close AI wizard' : 'Create with AI'}
          </button>}
          {canEditKnowledge && <button type="button" className="btn btn-primary" disabled={working !== null} onClick={showCreate ? clearCreatePanels : openManualCreate}>
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

      {aiWizardVisible && canEditKnowledge && <KnowledgeAIWizard busy={working === 'knowledge-ai-draft'} onGenerate={generateKnowledgeDraft} onCancel={clearCreatePanels} />}
      {showCreate && <KnowledgeFormPanel mode="create" draft={generatedDraft} busy={working === 'create'} onSubmit={createKnowledgeBase} onCancel={clearCreatePanels} />}
      {showEdit && selected && <KnowledgeFormPanel key={selected.id} mode="edit" knowledge={selected} busy={working === 'update'} onSubmit={updateKnowledgeBase} onCancel={() => setShowEdit(false)} />}

      <section className={styles.stats} aria-label="Knowledge health">
        <Metric icon={BookOpenCheck} label="Knowledge bases" value={knowledgeBases.length} detail={`${knowledgeBases.filter((kb) => kb.approval_status === 'approved').length} approved`} />
        <Metric icon={BadgeCheck} label="Indexed sources" value={indexed} detail="Provider-confirmed" tone="success" />
        <Metric icon={RefreshCw} label="Processing" value={processing} detail="Awaiting provider" tone={processing ? 'warning' : 'neutral'} />
        <Metric
          icon={Bot}
          label="Bound agents"
          value={boundAgents}
          detail={agents.length > 0 && boundAgents === agents.length ? 'All agents have knowledge access' : `${agents.length - boundAgents} not yet bound`}
        />
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
          {canEditKnowledge && <button type="button" className="btn btn-primary" onClick={openManualCreate}><Plus size={14} /> Create first knowledge base</button>}
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
                    <div className={styles.eyebrowRow}><span className="page-kicker">{scopeLabel(selected)}</span><StatusBadge status={selected.sync_status} /><span className={`badge ${selected.approval_status === 'approved' ? 'badge-success' : 'badge-neutral'}`}>{selected.approval_status === 'approved' ? 'approved' : selected.serving_revision ? 'changes pending' : 'draft'}</span></div>
                    <h2 ref={detailHeadingRef} id="knowledge-detail-heading" tabIndex={-1}>{selected.name}</h2>
                    <p>{selected.description || 'No description added yet.'}</p>
                  </div>
                </div>
                <div className={styles.heroActions}>
                  {canEditKnowledge && <button type="button" className="btn btn-secondary btn-sm" disabled={working !== null} onClick={() => { setShowCreate(false); setShowAIWizard(false); setGeneratedDraft(null); setShowEdit(true); if (router.query.create === 'ai') void router.replace('/knowledge', undefined, { shallow: true }); }}><Pencil size={12} /> Edit details</button>}
                  {canGovernKnowledge && <button type="button" className="btn btn-ghost btn-sm" disabled={working !== null} onClick={deleteSelected} aria-label={`Delete ${selected.name}`}><Trash2 size={13} /> Delete</button>}
                </div>
                <div className={styles.progressRail} aria-label={`${selected.indexed_source_count} of ${selected.source_count} documents with extracted text`}>
                  <div style={{ width: `${selected.source_count ? Math.round(selected.indexed_source_count / selected.source_count * 100) : 0}%` }} />
                </div>
                <div className={styles.heroMeta}>
                  <span><strong>{selected.indexed_source_count}/{selected.source_count}</strong> extracted</span>
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
                  <SourceTab active={sourceMode === 'crawl'} icon={Globe2} label="Crawl website" onClick={() => setSourceMode('crawl')} />
                  <SourceTab active={sourceMode === 'urls'} icon={Globe2} label="Web pages" onClick={() => setSourceMode('urls')} />
                  <SourceTab active={sourceMode === 'sitemap'} icon={Link2} label="Sitemap" onClick={() => setSourceMode('sitemap')} />
                  <SourceTab active={sourceMode === 'pdf'} icon={FileText} label="PDF" onClick={() => setSourceMode('pdf')} />
                  <SourceTab active={sourceMode === 'text'} icon={Layers3} label="Text" onClick={() => setSourceMode('text')} />
                </div>
                <div className={styles.sourceBuilder} role="tabpanel">
                  {sourceMode === 'crawl' && <CrawlForm busy={working === 'crawl-site'} onSubmit={startSiteCrawl} />}
                  {sourceMode === 'urls' && <UrlForm busy={working === 'add-urls'} onSubmit={addUrls} />}
                  {sourceMode === 'sitemap' && <SitemapForm busy={working === 'discover-sitemap' || working === 'index-sitemap'} onSubmit={discoverSitemap} urls={sitemapUrls} selected={selectedSitemapUrls} onToggle={(url) => setSelectedSitemapUrls((current) => { const next = new Set(current); if (next.has(url)) next.delete(url); else next.add(url); return next; })} onToggleAll={() => setSelectedSitemapUrls((current) => current.size === sitemapUrls.length ? new Set() : new Set(sitemapUrls))} onIndex={indexSitemapSelection} />}
                  {sourceMode === 'pdf' && <PdfForm busy={working === 'upload-pdf'} onSubmit={uploadPdf} />}
                  {sourceMode === 'text' && <TextForm busy={working === 'add-text'} onSubmit={addText} />}
                </div>
              </section>}

              {selected.crawls.length > 0 && (
                <CrawlRuns
                  crawls={selected.crawls}
                  busy={working !== null}
                  canRepair={canEditKnowledge}
                  onRetry={retrySiteCrawl}
                />
              )}

              <SourcesSection
                sources={selected.sources}
                canRepair={canEditKnowledge}
                canRemove={canGovernKnowledge}
                busy={working !== null}
                onRepair={repairSource}
                onCompile={compileSource}
                onRemove={removeSource}
              />

              <section className={styles.section} aria-labelledby="governance-heading">
                <div className={styles.sectionHeading}>
                  <div><span className={styles.sectionIcon}><ShieldCheck size={15} /></span><div><h3 id="governance-heading">Approval & agent access</h3><p>Calls use the last approved immutable release while newer changes remain in draft.</p></div></div>
                </div>
                <div className={styles.governanceGrid}>
                  <div className={styles.approvalCard}>
                    <span className={styles.miniLabel}>Release gate</span>
                    <strong>{selected.approval_status === 'approved' ? 'Approved for agent use' : selected.serving_revision ? 'Draft changes pending approval; last approved release remains live' : 'Draft—not available to agents'}</strong>
                    {selected.serving_revision && (
                      <p>
                        Live release {selected.serving_revision.revision_id.slice(0, 12)} · {selected.serving_revision.source_count} sources · {selected.serving_revision.chunk_count} retrieval chunks · published {new Date(selected.serving_revision.published_at).toLocaleString()}. Calls remain pinned to this immutable release until the draft passes approval.
                      </p>
                    )}
                    <p>{selected.sync_status === 'ready' ? 'Every source has extracted text. Publication also checks known extraction failures and representative retrieval for a declared company.' : 'Make every source VAV-searchable before approval becomes available.'}</p>
                    {selected.speech_lexicon ? (
                      <p role="status">
                        Voice recognition artifact {selected.speech_lexicon.artifact_id.slice(0, 12)} · {selected.speech_lexicon.entry_count} complete terms · {typeof selected.speech_lexicon.coverage.tier_one_coverage_pct === 'number' ? `${selected.speech_lexicon.coverage.tier_one_coverage_pct.toFixed(1)}% critical-name coverage` : 'critical-name coverage unavailable'}. It is pinned to this approved source revision.
                      </p>
                    ) : selected.approval_status === 'approved' ? (
                      <p role="status">This legacy approval is awaiting its versioned voice-recognition backfill. Calls retain the safe legacy terminology path until publication.</p>
                    ) : (
                      <p>The versioned voice-recognition artifact is compiled automatically when this exact source revision is approved.</p>
                    )}
                    {canGovernKnowledge && selected.approval_status !== 'approved' && <button type="button" className="btn btn-primary btn-sm" disabled={working !== null || selected.sync_status !== 'ready'} onClick={() => runAction('approve', () => approveWithCoverageAcknowledgement(selected.id), 'Knowledge approved for agent binding.')}>
                      <Check size={12} /> Approve knowledge
                    </button>}
                    {canGovernKnowledge && selected.serving_revision && <button type="button" className="btn btn-danger btn-sm" disabled={working !== null} onClick={() => runAction('revoke-live', () => api.approveKnowledgeBase(selected.id, false), 'Live release revoked. New calls cannot use this knowledge; calls already in progress keep their immutable release.')}>
                      <X size={12} /> Revoke live release now
                    </button>}
                    {canGovernKnowledge && selected.approval_status === 'approved' && !selected.serving_revision && <button type="button" className="btn btn-secondary btn-sm" disabled={working !== null} onClick={() => runAction('revoke-legacy', () => api.approveKnowledgeBase(selected.id, false), 'Legacy approval removed; bound agents require review.')}>
                      <X size={12} /> Return legacy approval to draft
                    </button>}
                    {canGovernKnowledge && rollbackCandidates.length > 0 && (
                      <details className={styles.releaseHistory}>
                        <summary>Restore a previous VAV release</summary>
                        <p>For incidents only. The immutable release and its matching voice-recognition artifact move together. A concurrent operator change is rejected.</p>
                        <form onSubmit={reactivateRelease}>
                          <label htmlFor="knowledge-release-rollback">Verified release</label>
                          <select id="knowledge-release-rollback" value={effectiveRollbackRevisionId} onChange={(event) => setRollbackRevisionId(event.target.value)} disabled={working !== null} required>
                            {rollbackCandidates.map((release) => (
                              <option key={release.revision_id} value={release.revision_id}>
                                {new Date(release.published_at).toLocaleString()} · {release.source_count} sources · {release.revision_id.slice(0, 12)}
                              </option>
                            ))}
                          </select>
                          <label htmlFor="knowledge-release-reason">Incident or rollback reason</label>
                          <input id="knowledge-release-reason" value={rollbackReason} onChange={(event) => setRollbackReason(event.target.value)} minLength={3} maxLength={500} placeholder="Why is this verified release safer?" disabled={working !== null} required />
                          <button type="submit" className="btn btn-secondary btn-sm" disabled={working !== null || !effectiveRollbackRevisionId || rollbackReason.trim().length < 3}>
                            {working === 'reactivate-release' ? <Loader2 className="spin" size={12} /> : <RefreshCw size={12} />} Reactivate for new calls
                          </button>
                        </form>
                      </details>
                    )}
                  </div>
                  <AgentBinding selected={selected} agents={agents} busy={working !== null} canManage={canGovernKnowledge} onBind={(agentId) => runAction('bind', () => api.bindKnowledgeAgent(selected.id, agentId), 'Agent binding saved. VAV-native agents retrieve the approved release immediately.')} onUnbind={(agentId) => runAction('unbind', () => api.unbindKnowledgeAgent(selected.id, agentId), 'Agent was unbound.')} />
                </div>
              </section>
            </div>
          )}
        </div>
      )}
    </Layout>
  );
}

function KnowledgeFormPanel({ mode, knowledge, draft, busy, onSubmit, onCancel }: { mode: 'create' | 'edit'; knowledge?: KnowledgeBase; draft?: KnowledgeAIDraftResponse | null; busy: boolean; onSubmit: (event: FormEvent<HTMLFormElement>) => void; onCancel: () => void }) {
  const editing = mode === 'edit';
  const generated = editing ? null : draft?.draft;
  return <section className={styles.createPanel} aria-labelledby="create-knowledge-heading">
    <div className={styles.createAside}>
      <Sparkles size={21} />
      <span className="page-kicker">{editing ? 'Keep context accurate' : draft ? 'Review the OpenAI draft' : 'Govern from day one'}</span>
      <h2 id="create-knowledge-heading">{editing ? 'Edit knowledge details' : draft ? 'Review knowledge metadata' : 'Create a reusable knowledge base'}</h2>
      <p>{editing ? 'Update business scope, languages, and discovery metadata without losing source provenance or bindings.' : draft ? draft.rationale : 'Scope it to the correct business level. Sources and agent access are added after creation.'}</p>
      {draft?.assumptions.length ? <><strong>Assumptions to verify</strong><ul>{draft.assumptions.map((item) => <li key={item}>{item}</li>)}</ul></> : null}
      {draft?.recommended_sources.length ? <><strong>Recommended approved sources</strong><ul>{draft.recommended_sources.map((item) => <li key={item}>{item}</li>)}</ul></> : null}
      <div><span><ShieldCheck size={14} /> Tenant isolated</span><span><Layers3 size={14} /> Provider neutral</span><span><BadgeCheck size={14} /> Approval gated</span></div>
    </div>
    <form className={styles.createForm} onSubmit={onSubmit}>
      <div className="form-grid">
        <div className="form-group"><label htmlFor="kb-name">Name <span>Required</span></label><input id="kb-name" name="name" maxLength={40} required defaultValue={knowledge?.name || generated?.name || ''} placeholder="Company product and policy knowledge" /></div>
        <div className="form-group"><label htmlFor="kb-scope">Business scope</label><select id="kb-scope" name="scope_type" defaultValue={knowledge?.scope_type || generated?.scope_type || 'workspace'}>{scopeOptions.map((scope) => <option key={scope.value} value={scope.value}>{scope.label}</option>)}</select></div>
      </div>
      <div className="form-group"><label htmlFor="kb-description">Purpose</label><textarea id="kb-description" name="description" maxLength={1000} defaultValue={knowledge?.description || generated?.description || ''} placeholder="Approved product descriptions, delivery policies, returns and customer FAQs." /></div>
      <div className="form-grid">
        <div className="form-group"><label htmlFor="kb-scope-label">Scope name</label><input id="kb-scope-label" name="scope_label" maxLength={255} defaultValue={knowledge?.scope_label || generated?.scope_label || ''} placeholder="E-commerce division" /></div>
        <div className="form-group"><label htmlFor="kb-owner">Company this knowledge belongs to</label><input id="kb-owner" name="owner_company" maxLength={160} defaultValue={knowledge?.owner_company || ''} placeholder="Exact company name configured on the agent" /><small>For a single-company knowledge base, all approved sources belong to this company, including doctors and departments. Leave blank for mixed-company knowledge; those facts need individual attribution. Changing this requires publication.</small></div>
        <div className="form-group"><label htmlFor="kb-languages">Languages <span>Comma separated</span></label><input id="kb-languages" name="languages" defaultValue={knowledge?.languages.join(', ') || generated?.languages.join(', ') || 'en'} placeholder="en, ar, hi, ml" /></div>
      </div>
      <div className="form-group"><label htmlFor="kb-tags">Tags <span>Comma separated</span></label><input id="kb-tags" name="tags" defaultValue={knowledge?.tags.join(', ') || generated?.tags.join(', ') || ''} placeholder="products, delivery, returns" /></div>
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
  return <form onSubmit={onSubmit} className={styles.uploadForm}><div className={styles.uploadDrop}><Upload size={22} /><div><label htmlFor="knowledge-pdf">Choose an approved PDF</label><p>VAV extracts text, OCRs scanned pages and compiles source-backed knowledge · original PDF preserved · same filename updates one document · maximum 8 MB</p></div><input id="knowledge-pdf" name="media" type="file" accept="application/pdf,.pdf" required /></div><ProcessingModeField /><button type="submit" className="btn btn-primary" disabled={busy}>{busy ? <Loader2 className="spin" size={14} /> : <CloudUpload size={14} />} {busy ? 'Extracting, compiling and indexing…' : 'Validate and index'}</button>{busy && <p role="status">OCR and AI compilation can take time for large documents. Keep this page open; the approved release is unchanged.</p>}</form>;
}

function ProcessingModeField() {
  return <label><span>Knowledge processing</span><select name="processing_mode" defaultValue="automatic"><option value="automatic">Automatic · AI with safe text fallback</option><option value="fast">Fast · searchable text, no AI facts</option><option value="ai_verified">AI · source-checked facts</option></select><small>AI uses your configured OpenAI account. Review warnings and approve before publishing.</small></label>;
}

function CrawlForm({ busy, onSubmit }: { busy: boolean; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) {
  return <form onSubmit={onSubmit} className={styles.crawlForm}>
    <div className={styles.crawlIntro}><label htmlFor="homepage-url">Website homepage</label><p>VAV follows sitemaps and same-site links, respects robots.txt, renders JavaScript when needed, and keeps a repair ledger for every page.</p></div>
    <input id="homepage-url" name="homepage_url" type="url" required placeholder="https://www.example.com/" />
    <ProcessingModeField />
    <label><span>Page limit</span><input name="max_pages" type="number" min="1" max="500" defaultValue="100" /></label>
    <label><span>Link depth</span><input name="max_depth" type="number" min="0" max="8" defaultValue="3" /></label>
    <label className={styles.crawlCheckbox}><input name="include_subdomains" type="checkbox" /> Include subdomains</label>
    <button type="submit" className="btn btn-primary" disabled={busy}>{busy ? <Loader2 className="spin" size={14} /> : <Search size={14} />} Crawl and index</button>
  </form>;
}

function CrawlRuns({ crawls, busy, canRepair, onRetry }: { crawls: KnowledgeCrawl[]; busy: boolean; canRepair: boolean; onRetry: (crawl: KnowledgeCrawl) => void }) {
  return <section className={styles.section} aria-labelledby="crawl-runs-heading">
    <div className={styles.sectionHeading}><div><span className={styles.sectionIcon}><Globe2 size={15} /></span><div><h3 id="crawl-runs-heading">Website crawl activity</h3><p>Discovery, extraction, provider indexing and page-level failures remain traceable.</p></div></div><span className="badge badge-neutral">{crawls.length} run{crawls.length === 1 ? '' : 's'}</span></div>
    <div className={styles.crawlRuns}>{crawls.slice(0, 5).map((crawl) => {
      const excluded = crawl.pages.filter((page) => page.status === 'skipped');
      const terminal = crawl.indexed_count + crawl.failed_count + excluded.length;
      const percent = crawl.discovered_count ? Math.round((terminal / crawl.discovered_count) * 100) : 0;
      const active = ['queued', 'discovering', 'indexing', 'retrying'].includes(crawl.status);
      const warnings = Array.isArray(crawl.options?.warnings) ? crawl.options.warnings.filter((item): item is string => typeof item === 'string') : [];
      const processingMode = typeof crawl.options?.processing_mode === 'string' ? crawl.options.processing_mode : 'automatic';
      const failures = crawl.pages.filter((page) => page.status === 'failed');
      return <article className={styles.crawlRun} key={crawl.id}>
        <div className={styles.crawlRunHeader}><div><strong>{crawl.allowed_host}</strong><span>{crawl.root_url} · {processingModeLabel(processingMode)}</span></div><span className={`badge ${crawl.failed_count || crawl.status === 'failed' ? 'badge-danger' : crawl.status === 'completed' ? 'badge-success' : 'badge-warning'}`}>{crawlStatusLabel(crawl.status)}</span></div>
        <div className={styles.crawlStats}><span><strong>{crawl.discovered_count}</strong> discovered</span><span><strong>{crawl.indexed_count}</strong> ready</span><span><strong>{crawl.queued_count}</strong> processing</span><span><strong>{crawl.failed_count}</strong> failed</span><span><strong>{crawl.skipped_count}</strong> skipped</span></div>
        <div className={styles.crawlProgress} aria-label={`${percent}% of discovered pages processed`}><div style={{ width: `${percent}%` }} /></div>
        {active && <p className={styles.crawlMessage}><Loader2 className="spin" size={12} /> {crawl.status === 'discovering' ? 'Discovering sitemaps and same-site links…' : 'Extracting, indexing and verifying discovered pages…'}</p>}
        {crawl.error_message && <p className={styles.crawlError}><CircleAlert size={12} /> {crawl.error_message}</p>}
        {warnings.map((warning) => <p className={styles.crawlWarning} key={warning}>{warning}</p>)}
        {excluded.length > 0 && <details className={styles.crawlFailures}><summary>{excluded.length} non-content page{excluded.length === 1 ? '' : 's'} excluded automatically</summary><ul>{excluded.slice(0, 50).map((page) => <li key={page.id}><span>{page.url}</span><small>{page.error_message || 'No useful voice-searchable content'}</small></li>)}</ul></details>}
        {failures.length > 0 && <details className={styles.crawlFailures}><summary>{failures.length} failed page{failures.length === 1 ? '' : 's'} — inspect details</summary><ul>{failures.slice(0, 50).map((page) => <li key={page.id}><span>{page.url}</span><small>{page.error_code ? `${page.error_code}: ` : ''}{page.error_message || 'Recovery failed'}</small></li>)}</ul></details>}
        {canRepair && !active && (crawl.failed_count > 0 || crawl.status === 'failed') && <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => onRetry(crawl)}><RefreshCw size={12} /> {crawl.failed_count ? 'Repair failed pages' : 'Retry discovery'}</button>}
      </article>;
    })}</div>
  </section>;
}

function TextForm({ busy, onSubmit }: { busy: boolean; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) {
  return <form onSubmit={onSubmit} className={styles.builderForm}><div><label htmlFor="text-name">Approved searchable text</label><p>Compiled for review before Inworld, Sarvam, and ElevenLabs VAV runtimes use it.</p></div><input id="text-name" name="text_name" required maxLength={255} placeholder="Approved returns FAQ" /><textarea id="text-content" name="text_content" required minLength={20} maxLength={100000} placeholder="Paste approved question-and-answer content here…" /><ProcessingModeField /><button type="submit" className="btn btn-secondary" disabled={busy}>{busy ? <Loader2 className="spin" size={14} /> : <Plus size={14} />} {busy ? 'Compiling and validating…' : 'Add searchable text'}</button>{busy && <p role="status">Preparing source-backed knowledge. Your text and the approved release remain unchanged.</p>}</form>;
}

function SourceReview({ source }: { source: KnowledgeSource }) {
  const [preview, setPreview] = useState<Awaited<ReturnType<typeof api.previewKnowledgeSource>> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loadPreview = async () => {
    if (preview || loading) return;
    setLoading(true);
    setError(null);
    try {
      setPreview(await api.previewKnowledgeSource(source.knowledge_base_id, source.id));
    } catch (err) {
      setError(errorMessage(err, 'Could not load source. Close and reopen to retry.'));
    } finally {
      setLoading(false);
    }
  };
  return <details onToggle={(event) => { if (event.currentTarget.open) void loadPreview(); }}>
    <summary>Review extracted content and facts</summary>
    {loading && <p role="status">Loading source evidence…</p>}
    {error && <p role="alert">{error}</p>}
    {preview && <div style={{ maxHeight: 360, overflow: 'auto' }}>
      <p>Editable source preview. Pending changes require approval; calls use the approved release. Source checking does not guarantee that every fact was extracted.</p>
      {(preview.structured_content.facts || []).map((fact, index) => <div key={index}>
        <strong>{fact.subject} — {fact.predicate}: {fact.value}</strong>
        <blockquote>{fact.evidence}</blockquote>
      </div>)}
      {!(preview.structured_content.facts || []).length && <p>No structured facts. Check extraction and company attribution before approval.</p>}
      <h4>Original extracted text</h4>
      <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{preview.raw_text || 'Original extraction unavailable; re-upload this source.'}</pre>
    </div>}
  </details>;
}

function SourcesSection({ sources, canRepair, canRemove, busy, onRepair, onCompile, onRemove }: { sources: KnowledgeSource[]; canRepair: boolean; canRemove: boolean; busy: boolean; onRepair: (source: KnowledgeSource) => void; onCompile: (source: KnowledgeSource) => void; onRemove: (source: KnowledgeSource) => void }) {
  const readyCount = sources.filter((source) => source.quality_status === 'sample_checks_passed').length;
  return <section className={styles.section} aria-labelledby="sources-heading"><div className={styles.sectionHeading}><div><span className={styles.sectionIcon}><Layers3 size={15} /></span><div><h3 id="sources-heading">Source inventory</h3><p>Extraction is not answer readiness. Each document shows extraction issues and whether representative company-scoped retrieval checks passed.</p></div></div><span className="badge badge-neutral">{sources.length} documents · {readyCount} passed sample checks</span></div>{sources.length === 0 ? <div className={styles.sourceEmpty}><FileText size={20} /><div><strong>No sources yet</strong><p>Add curated web pages, searchable text, or an approved PDF to begin.</p></div></div> : <div className={styles.sourceList}>{sources.map((source) => {
    const method = typeof source.source_metadata?.extraction_method === 'string' ? source.source_metadata.extraction_method : null;
    const compiler = sourceCompiler(source);
    const compilation = sourceUploadCompilation(source);
    const compilationActive = compilation?.status === 'queued' || compilation?.status === 'processing';
    const isReady = source.quality_status === 'sample_checks_passed';
    const recovery = sourceRecovery(source);
    const isWebsite = source.source_type === 'url' || source.source_type === 'website' || source.source_type === 'sitemap';
    // Ready web pages may still need a deliberate refresh when their website
    // changes or a better extraction strategy becomes available.
    const repairable = isWebsite;
    const recoveryDetail = compilation ? compilation.message : recovery?.status === 'queued' || recovery?.status === 'processing'
      ? recovery.message || `VAV is ${recoveryStageLabel(recovery.stage)}…`
      : recovery?.status === 'failed' && isReady
        ? recovery.message || 'Latest refresh failed; the previous approved content remains active.'
      : !source.retrieval_ready
        ? isWebsite ? 'VAV could not read this page yet. Use Repair page to recover it automatically.' : 'VAV cannot use this document yet. Re-upload it to run extraction and OCR repair.'
        : null;
    const detail = [recoveryDetail, ...(source.quality_issues || ['Retrieval has not been tested. Extracted text alone does not prove answer readiness.'])].filter(Boolean).join(' ');
    const recoveryActive = recovery?.status === 'queued' || recovery?.status === 'processing';
    const refreshLabel = isReady ? 'Refresh page' : 'Repair page';
return <article className={styles.sourceRow} key={source.id}><span className={styles.sourceTypeIcon}>{source.source_type === 'file' ? <FileText size={16} /> : source.source_type === 'text' ? <Layers3 size={16} /> : <Globe2 size={16} />}</span><div className={styles.sourceIdentity}><strong>{source.name}</strong><span>{source.location || (source.size_bytes ? formatBytes(source.size_bytes) : source.source_type)} · {source.retrieval_ready ? `${source.extracted_character_count.toLocaleString()} extracted characters${method ? ` · ${method === 'native' ? 'text extracted' : method === 'static_html' ? 'HTML extracted' : method === 'javascript_render' ? 'JavaScript rendered' : method}` : ''}${compiler ? ` · ${compilerLabel(compiler)}` : ''}` : 'no voice-searchable text'}</span>{compiler?.warning && <p>{compiler.warning}</p>}{uncoveredList(source)}{detail && <p className={recoveryActive || compilationActive ? styles.recoveryProgress : undefined}>{detail}</p>}{source.error_message && source.error_message !== detail && <p>{source.error_message}</p>}<SourceReview key={`${source.id}:${source.updated_at}`} source={source} /></div><span className={`badge ${isReady ? 'badge-success' : source.quality_status === 'needs_repair' ? 'badge-danger' : 'badge-neutral'}`}>{compilationActive ? (compilation?.status === 'queued' ? 'Queued' : 'Processing AI') : isReady ? 'Sample checks passed' : recoveryActive ? recoveryStageLabel(recovery.stage) : source.quality_status === 'needs_repair' ? 'Needs repair' : source.retrieval_ready ? 'Extracted · not verified' : source.status.replace('_', ' ')}</span><time>{formatDate(source.last_synced_at || source.updated_at)}</time><span className={styles.sourceActions}>{canRepair && repairable && <button type="button" className="btn btn-secondary btn-sm" disabled={busy || recoveryActive} onClick={() => onRepair(source)} aria-label={`${refreshLabel} ${source.name} and re-index its searchable content`} title="Download, render, extract, index and verify the latest page content"><RefreshCw size={12} className={recoveryActive ? 'spin' : undefined} /> {refreshLabel}</button>}{canRepair && !isWebsite && (source.retrieval_ready || Boolean(compilation)) && <button type="button" className="btn btn-secondary btn-sm" disabled={busy || compilationActive} onClick={() => onCompile(source)} aria-label={`Structure ${source.name} with AI`} title="Compile the original into source-checked VAV facts; review before approval">{compilationActive ? 'Processing…' : busy ? 'Working…' : compilation?.status === 'failed' ? 'Retry processing' : 'Structure with AI'}</button>}{canRemove && <button type="button" className="icon-button" disabled={busy} onClick={() => onRemove(source)} aria-label={`Stage removal of ${source.name}`} title="Stage removal for the next approved VAV release"><Trash2 size={14} /></button>}</span></article>;
  })}</div>}</section>;
}

function AgentBinding({ selected, agents, busy, canManage, onBind, onUnbind }: { selected: KnowledgeBase; agents: VoiceAgent[]; busy: boolean; canManage: boolean; onBind: (agentId: string) => void; onUnbind: (agentId: string) => void }) {
  const [agentId, setAgentId] = useState('');
  const boundIds = new Set(selected.agent_bindings.map((binding) => binding.agent_id));
  const available = agents.filter((agent) => !boundIds.has(agent.id));
  const selectedAgent = available.find((agent) => agent.id === agentId);
  const eligible = available.filter((agent) => canBindKnowledgeAgent(selected, agent));
  const guidance = knowledgeBindingGuidance(selected);
  return <div className={styles.bindingCard}><div className={styles.bindingHeader}><div><span className={styles.miniLabel}>Agent access</span><strong>{selected.agent_bindings.length} agents bound</strong></div><Bot size={18} /></div><div className={styles.bindingList}>{selected.agent_bindings.map((binding) => <div key={binding.id}><span className={styles.agentAvatar}><Bot size={13} /></span><span><strong>{binding.agent_name}</strong><small>{binding.sync_status === 'synced' ? 'Live knowledge retrieval' : isVavNativeKnowledgeProvider(binding.agent_voice_provider) ? 'Approve the knowledge base to make this binding live' : 'Not live: this agent does not use VAV retrieval. Move it to Inworld, Sarvam or ElevenLabs, or unbind it.'}</small></span>{canManage && <button type="button" className="icon-button" disabled={busy} onClick={() => onUnbind(binding.agent_id)} aria-label={`Unbind ${binding.agent_name}`}><Unlink size={14} /></button>}</div>)}{selected.agent_bindings.length === 0 && <p className={styles.bindingEmpty}>No agents can use this knowledge yet.</p>}</div>{canManage && <div className={styles.bindControl}><select aria-label="Agent to bind" value={agentId} disabled={busy || eligible.length === 0} onChange={(event) => setAgentId(event.target.value)}><option value="">Select an agent…</option>{available.map((agent) => <option key={agent.id} value={agent.id} disabled={!canBindKnowledgeAgent(selected, agent)}>{agent.name}</option>)}</select><button type="button" className="btn btn-secondary btn-sm" disabled={busy || !agentId || !canBindKnowledgeAgent(selected, selectedAgent)} onClick={() => { onBind(agentId); setAgentId(''); }}><Link2 size={12} /> Bind</button>{guidance && <p className="form-hint">{guidance}</p>}</div>}</div>;
}

function StatusBadge({ status }: { status: KnowledgeBase['sync_status'] }) {
  const labels: Record<KnowledgeBase['sync_status'], string> = { local_only: 'Local draft', processing: 'Processing', ready: 'Text extracted', error: 'Needs attention' };
  return <span className={`badge ${status === 'error' ? 'badge-danger' : status === 'processing' ? 'badge-warning' : 'badge-neutral'}`}>{labels[status]}</span>;
}

function StatusDot({ status }: { status: KnowledgeBase['sync_status'] }) { return <span className={`${styles.statusDot} ${styles[`status_${status}`]}`} title={status.replace('_', ' ')} />; }
function scopeLabel(kb: KnowledgeBase) { return kb.scope_label || scopeOptions.find((scope) => scope.value === kb.scope_type)?.label || kb.scope_type; }
type SourceRecovery = { status?: string; stage?: string; message?: string };
function sourceUploadCompilation(source: KnowledgeSource): SourceRecovery | null { const value = source.source_metadata?.upload_compile; return value && typeof value === 'object' ? value as SourceRecovery : null; }
type SourceCompiler = { effective_mode?: string; model?: string; input_tokens?: number; output_tokens?: number; estimated_cost_usd?: number; estimated_cost_aed?: number; reused?: boolean; warning?: string | null };
type SourceCoverage = {
  status: string;
  record_total: number;
  records_covered: number;
  uncovered: string[];
  uncovered_total: number;
  facts_accepted: number;
};

function sourceCoverage(source: KnowledgeSource): SourceCoverage | null {
  const raw = source.source_metadata?.coverage as Partial<SourceCoverage> | undefined;
  if (!raw || typeof raw !== 'object' || typeof raw.status !== 'string') return null;
  return {
    status: raw.status,
    record_total: Number(raw.record_total || 0),
    records_covered: Number(raw.records_covered || 0),
    uncovered: Array.isArray(raw.uncovered) ? raw.uncovered.map(String) : [],
    uncovered_total: Number(raw.uncovered_total || 0),
    facts_accepted: Number(raw.facts_accepted || 0),
  };
}

function coverageNoticeType(source: KnowledgeSource): 'success' | 'info' {
  const coverage = sourceCoverage(source);
  return coverage && (coverage.status === 'complete' || coverage.status === 'unstructured') ? 'success' : 'info';
}

function coverageNotice(source: KnowledgeSource): string {
  const coverage = sourceCoverage(source);
  if (!coverage) return `Recovered ${source.name}: readable text was extracted and compiled. Review its coverage before approval.`;
  if (coverage.status === 'complete') return `Recovered ${source.name}: all ${coverage.record_total} records were captured as ${coverage.facts_accepted} verified facts.`;
  if (coverage.status === 'unstructured') return `Recovered ${source.name}: ${coverage.facts_accepted} verified facts were extracted from prose. The page has no cards, rows or lists to measure completeness against.`;
  if (coverage.status === 'partial') return `Recovered ${source.name}, but coverage is partial: ${coverage.records_covered} of ${coverage.record_total} records were captured. Open “Not captured” under the source to see every missing record before approval.`;
  return `Recovered ${source.name}: text was extracted. ${coverage.status === 'skipped' ? 'Coverage was not measured because fast extraction was requested.' : 'AI compilation did not run, so its facts are not verified.'}`;
}

function uncoveredList(source: KnowledgeSource) {
  const coverage = sourceCoverage(source);
  if (!coverage || coverage.status !== 'partial' || coverage.uncovered.length === 0) return null;
  const hidden = Math.max(0, coverage.uncovered_total - coverage.uncovered.length);
  return <details className="form-hint"><summary>Not captured: {coverage.uncovered_total} of {coverage.record_total} records</summary><ul>{coverage.uncovered.map((item, index) => <li key={`${source.id}-uncovered-${index}`}>{item}</li>)}</ul>{hidden > 0 && <p>{hidden} more records are not listed.</p>}</details>;
}

function sourceRecovery(source: KnowledgeSource): SourceRecovery | null { const value = source.source_metadata?.recovery; return value && typeof value === 'object' ? value as SourceRecovery : null; }
function sourceCompiler(source: KnowledgeSource): SourceCompiler | null { const value = source.source_metadata?.compiler; return value && typeof value === 'object' ? value as SourceCompiler : null; }
function compilerLabel(value: SourceCompiler) { if (value.reused) return 'unchanged · previous compilation reused'; if (value.effective_mode !== 'ai_verified') return 'deterministic knowledge'; const tokens = Number(value.input_tokens || 0) + Number(value.output_tokens || 0); const usd = Number(value.estimated_cost_usd || 0); const aed = Number(value.estimated_cost_aed || usd * 3.6725); return `AI-verified${value.model ? ` by ${value.model}` : ''} · ${tokens.toLocaleString()} tokens · $${usd.toFixed(4)} / AED ${aed.toFixed(4)}`; }
function processingModeLabel(value: string) { const labels: Record<string, string> = { automatic: 'Automatic knowledge processing', fast: 'Fast deterministic processing', ai_verified: 'AI-verified processing' }; return labels[value] || labels.automatic; }
function recoveryStageLabel(stage?: string) { const labels: Record<string, string> = { queued: 'Queued', fetching: 'Downloading', rendering: 'Rendering JavaScript', extracting: 'Extracting text', compiling: 'Structuring knowledge', provider_indexing: 'Indexing', verifying: 'Verifying index', verified: 'Verified', failed: 'Needs attention' }; return labels[stage || ''] || 'Repairing'; }
function crawlStatusLabel(status: KnowledgeCrawl['status']) { const labels: Record<KnowledgeCrawl['status'], string> = { queued: 'Queued', discovering: 'Discovering', indexing: 'Indexing', retrying: 'Repairing', completed: 'Complete', completed_with_errors: 'Needs repair', failed: 'Discovery failed', cancelled: 'Cancelled' }; return labels[status]; }
function formatBytes(bytes: number) { return bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`; }
function formatDate(value: string) { return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)); }
async function approveWithCoverageAcknowledgement(knowledgeBaseId: string) {
  try {
    return await api.approveKnowledgeBase(knowledgeBaseId, true);
  } catch (error) {
    const message = errorMessage(error, '');
    if (!message.includes('accept_partial_coverage')) throw error;
    const summary = message.split(' Review the uncovered records')[0];
    const acknowledged = window.confirm(
      `${summary}\n\nApprove anyway? Callers will not get answers for the uncovered records until the source is fixed and recompiled.`,
    );
    if (!acknowledged) throw new Error('Approval cancelled. Review the uncovered records shown on each source, then approve again.');
    return api.approveKnowledgeBase(knowledgeBaseId, true, true);
  }
}

function errorMessage(error: unknown, fallback: string) { return error instanceof Error ? error.message : fallback; }
