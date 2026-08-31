import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from 'react';
import {
  AudioLines,
  FileText,
  FilterX,
  Globe2,
  ListChecks,
  PhoneOutgoing,
  Plus,
  RefreshCw,
  Search,
  Wrench,
  X,
} from 'lucide-react';
import Layout from '@/components/Layout';
import { isAgentCallReady } from '@/lib/agent-readiness.cjs';
import {
  transcriptLanguage,
  valueFromRecord,
} from '@/lib/conversation-ui.cjs';
import {
  api,
  CallRecord,
  CallSummary,
  CallTranscript,
  RuntimeProfile,
  VoiceAgent,
} from '@/lib/api';
import styles from '@/styles/conversation-operations.module.css';

type CallFilters = {
  query: string;
  direction: string;
  status: string;
  agentId: string;
  language: string;
};

type ToolEvent = {
  id: string;
  name: string;
  status: string;
};

const EMPTY_FILTERS: CallFilters = {
  query: '',
  direction: '',
  status: '',
  agentId: '',
  language: '',
};

function statusBadge(status: string) {
  const map: Record<string, string> = {
    completed: 'badge-success',
    in_progress: 'badge-info',
    ringing: 'badge-warning',
    dispatching: 'badge-warning',
    dispatch_unknown: 'badge-danger',
    failed: 'badge-danger',
    no_answer: 'badge-warning',
    busy: 'badge-warning',
  };
  return map[status] || 'badge-info';
}

function formatDate(value: string | null) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

function formatDuration(seconds: number | null) {
  if (seconds === null) return '—';
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
}

function detailError(error: unknown, subject: string) {
  const message = error instanceof Error ? error.message : `Could not load ${subject.toLowerCase()}.`;
  return message.toLowerCase().includes('not found')
    ? `${subject} is not available for this conversation.`
    : message;
}

function createDialIntentKey() {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  if (typeof globalThis.crypto?.getRandomValues === 'function') {
    const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  throw new Error('Secure call-attempt identification is unavailable in this browser.');
}

function callMetadata(call: CallRecord | null) {
  if (!call) return {};
  const metadata = (call as CallRecord & { call_metadata?: unknown }).call_metadata;
  return metadata && typeof metadata === 'object' && !Array.isArray(metadata)
    ? metadata as Record<string, unknown>
    : {};
}

function configuredLanguages(call: CallRecord | null) {
  const metadata = callMetadata(call);
  const languages = new Set<string>();
  const supported = metadata.supported_languages;
  if (Array.isArray(supported)) {
    supported.forEach((value) => {
      if (typeof value === 'string' && value.trim()) languages.add(value.trim());
    });
  }
  const primary = metadata.language;
  if (typeof primary === 'string' && primary.trim()) languages.add(primary.trim());
  return Array.from(languages);
}

function isBrowserConversation(call: CallRecord | null) {
  const metadata = callMetadata(call);
  return metadata.channel === 'browser' || metadata.conversation_type === 'webcall';
}

function languageName(code: string) {
  try {
    return new Intl.DisplayNames(['en'], { type: 'language' }).of(code) || code.toUpperCase();
  } catch {
    return code.toUpperCase();
  }
}

function providerVersion(call: CallRecord | null) {
  return valueFromRecord(callMetadata(call), [
    'agent_version',
    'agent_version_id',
    'provider_revision_id',
    'revision_id',
    'version_id',
  ]);
}

function transcriptToolEvents(transcript: CallTranscript | null): ToolEvent[] {
  if (!transcript) return [];
  return transcript.turns.flatMap((turn, index) => {
    const type = valueFromRecord(turn, ['type', 'event_type', 'kind']);
    const directName = valueFromRecord(turn, ['tool_name', 'function_name', 'tool']);
    const nestedTool = turn.tool && typeof turn.tool === 'object' && !Array.isArray(turn.tool)
      ? valueFromRecord(turn.tool, ['name', 'tool_name', 'function_name'])
      : '';
    const isToolEvent = type.toLowerCase().includes('tool') || type.toLowerCase().includes('function');
    const typedName = isToolEvent ? valueFromRecord(turn, ['name']) : '';
    const name = directName || nestedTool || typedName || (isToolEvent ? type : '');
    if (!name) return [];
    const status = valueFromRecord(turn, ['status', 'result_status', 'outcome']) || type || 'reported';
    return [{ id: `${index}-${name}-${status}`, name, status }];
  });
}

export default function Calls() {
  const [calls, setCalls] = useState<CallRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState('');
  const [historySyncing, setHistorySyncing] = useState(false);
  const [historySyncNotice, setHistorySyncNotice] = useState('');
  const [selectedCall, setSelectedCall] = useState<CallRecord | null>(null);
  const [transcript, setTranscript] = useState<CallTranscript | null>(null);
  const [summary, setSummary] = useState<CallSummary | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [transcriptError, setTranscriptError] = useState('');
  const [summaryError, setSummaryError] = useState('');
  const [recordingUrl, setRecordingUrl] = useState('');
  const [recordingLoading, setRecordingLoading] = useState(false);
  const [recordingError, setRecordingError] = useState('');
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [runtimeProfiles, setRuntimeProfiles] = useState<Record<string, RuntimeProfile>>({});
  const [showDialer, setShowDialer] = useState(false);
  const [dialing, setDialing] = useState(false);
  const [dialError, setDialError] = useState('');
  const [dialForm, setDialForm] = useState({ agent_id: '', to_number: '' });
  const [filters, setFilters] = useState<CallFilters>(EMPTY_FILTERS);
  const deferredQuery = useDeferredValue(filters.query);
  const dialIntentKeyRef = useRef<string | null>(null);
  const listRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  const dialogRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const lastFocusedRef = useRef<HTMLElement | null>(null);
  const recordingAudioRef = useRef<HTMLAudioElement>(null);
  const recordingObjectUrlRef = useRef<string | null>(null);
  const recordingRequestRef = useRef(0);

  const loadCallsAndAgents = useCallback(async () => {
    const requestId = listRequestRef.current + 1;
    listRequestRef.current = requestId;
    setLoading(true);
    setListError('');
    const [callsResult, agentsResult, runtimeResult] = await Promise.allSettled([
      api.listCalls({ page_size: '200' }),
      api.listAgents(),
      api.listRuntimeProfiles(),
    ]);
    if (listRequestRef.current !== requestId) return;
    if (callsResult.status === 'fulfilled') {
      setCalls(callsResult.value);
    } else {
      setListError(callsResult.reason instanceof Error ? callsResult.reason.message : 'Could not load conversations.');
    }
    if (agentsResult.status === 'fulfilled') setAgents(agentsResult.value);
    if (runtimeResult.status === 'fulfilled') {
      setRuntimeProfiles(Object.fromEntries(runtimeResult.value.map((profile) => [profile.agent_id, profile])));
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    let current = true;
    const requestId = listRequestRef.current + 1;
    listRequestRef.current = requestId;
    Promise.allSettled([
      api.listCalls({ page_size: '200' }),
      api.listAgents(),
      api.listRuntimeProfiles(),
    ]).then(([callsResult, agentsResult, runtimeResult]) => {
      if (!current || listRequestRef.current !== requestId) return;
      if (callsResult.status === 'fulfilled') {
        setCalls(callsResult.value);
      } else {
        setListError(callsResult.reason instanceof Error ? callsResult.reason.message : 'Could not load conversations.');
      }
      if (agentsResult.status === 'fulfilled') setAgents(agentsResult.value);
      if (runtimeResult.status === 'fulfilled') {
        setRuntimeProfiles(Object.fromEntries(runtimeResult.value.map((profile) => [profile.agent_id, profile])));
      }
      setLoading(false);
    });
    return () => {
      current = false;
      listRequestRef.current += 1;
    };
  }, []);

  const loadDetails = useCallback(async (callId: string) => {
    const requestId = detailRequestRef.current + 1;
    detailRequestRef.current = requestId;
    setTranscript(null);
    setSummary(null);
    setTranscriptError('');
    setSummaryError('');
    setDetailLoading(true);
    const [transcriptResult, summaryResult] = await Promise.allSettled([
      api.getCallTranscript(callId),
      api.getCallSummary(callId),
    ]);
    if (detailRequestRef.current !== requestId) return;
    if (transcriptResult.status === 'fulfilled') {
      setTranscript(transcriptResult.value);
    } else {
      setTranscriptError(detailError(transcriptResult.reason, 'Transcript'));
    }
    if (summaryResult.status === 'fulfilled') {
      setSummary(summaryResult.value);
    } else {
      setSummaryError(detailError(summaryResult.reason, 'Summary'));
    }
    setDetailLoading(false);
  }, []);

  const syncProviderHistory = async () => {
    setHistorySyncing(true);
    setHistorySyncNotice('');
    try {
      const result = await api.syncProviderConversationHistory();
      await loadCallsAndAgents();
      setHistorySyncNotice(
        result.failed
          ? `Recovered ${result.imported} conversations; ${result.failed} provider records could not be imported.`
          : `Provider history checked: ${result.imported} recovered, ${result.updated} refreshed.`,
      );
    } catch (error) {
      setHistorySyncNotice(
        error instanceof Error ? error.message : 'Provider conversation history could not be synced.',
      );
    } finally {
      setHistorySyncing(false);
    }
  };

  const releaseRecording = useCallback(() => {
    if (recordingAudioRef.current) {
      recordingAudioRef.current.pause();
      recordingAudioRef.current.removeAttribute('src');
      recordingAudioRef.current.load();
    }
    if (recordingObjectUrlRef.current) {
      URL.revokeObjectURL(recordingObjectUrlRef.current);
      recordingObjectUrlRef.current = null;
    }
  }, []);

  const resetRecording = useCallback(() => {
    recordingRequestRef.current += 1;
    releaseRecording();
    setRecordingUrl('');
    setRecordingLoading(false);
    setRecordingError('');
  }, [releaseRecording]);

  const openDetails = (call: CallRecord) => {
    resetRecording();
    setSelectedCall(call);
    void loadDetails(call.id);
  };

  const closeDetails = useCallback(() => {
    detailRequestRef.current += 1;
    resetRecording();
    setSelectedCall(null);
  }, [resetRecording]);

  useEffect(() => () => {
    recordingRequestRef.current += 1;
    releaseRecording();
  }, [releaseRecording]);

  const loadRecording = useCallback(async () => {
    if (!selectedCall?.recording_available) return;
    const callId = selectedCall.id;
    const requestId = recordingRequestRef.current + 1;
    recordingRequestRef.current = requestId;
    releaseRecording();
    setRecordingUrl('');
    setRecordingError('');
    setRecordingLoading(true);
    try {
      const audio = await api.getCallRecording(callId);
      if (recordingRequestRef.current !== requestId) return;
      if (!audio.size) throw new Error('The recording provider returned empty audio.');
      const objectUrl = URL.createObjectURL(audio);
      recordingObjectUrlRef.current = objectUrl;
      setRecordingUrl(objectUrl);
    } catch (error) {
      if (recordingRequestRef.current !== requestId) return;
      setRecordingError(
        error instanceof Error
          ? error.message
          : 'The recording could not be loaded securely.',
      );
    } finally {
      if (recordingRequestRef.current === requestId) setRecordingLoading(false);
    }
  }, [releaseRecording, selectedCall]);

  useEffect(() => {
    if (!selectedCall) return;

    lastFocusedRef.current = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    closeButtonRef.current?.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeDetails();
        return;
      }
      if (event.key !== 'Tab' || !dialogRef.current) return;

      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], audio[controls], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      document.body.style.overflow = previousOverflow;
      lastFocusedRef.current?.focus();
    };
  }, [closeDetails, selectedCall]);

  const initiateCall = async (event: React.FormEvent) => {
    event.preventDefault();
    setDialError('');
    setDialing(true);
    try {
      const intentKey = dialIntentKeyRef.current || createDialIntentKey();
      dialIntentKeyRef.current = intentKey;
      const call = await api.initiateCall(dialForm, intentKey);
      if (dialIntentKeyRef.current === intentKey) dialIntentKeyRef.current = null;
      setCalls((current) => [call, ...current.filter((item) => item.id !== call.id)]);
      setDialForm({ agent_id: '', to_number: '' });
      setShowDialer(false);
    } catch (error) {
      setDialError(error instanceof Error ? error.message : 'Could not start the call. Review the agent and number, then retry.');
    } finally {
      setDialing(false);
    }
  };

  const toggleDialer = () => {
    dialIntentKeyRef.current = null;
    setDialError('');
    setShowDialer((visible) => !visible);
  };

  const updateDialField = (field: 'agent_id' | 'to_number', value: string) => {
    dialIntentKeyRef.current = null;
    setDialError('');
    setDialForm((current) => ({ ...current, [field]: value }));
  };

  const agentMap = useMemo(
    () => new Map(agents.map((agent) => [agent.id, agent])),
    [agents],
  );
  const availableStatuses = useMemo(
    () => Array.from(new Set(calls.map((call) => call.status))).sort(),
    [calls],
  );
  const availableLanguages = useMemo(
    () => Array.from(new Set(calls.flatMap((call) => configuredLanguages(call)))).sort(),
    [calls],
  );
  const visibleCalls = useMemo(() => {
    const query = deferredQuery.trim().toLowerCase();
    return calls.filter((call) => {
      if (filters.direction && call.direction !== filters.direction) return false;
      if (filters.status && call.status !== filters.status) return false;
      if (filters.agentId && call.agent_id !== filters.agentId) return false;
      const languages = configuredLanguages(call);
      if (filters.language && !languages.includes(filters.language)) return false;
      if (!query) return true;
      const agentName = call.agent_id ? agentMap.get(call.agent_id)?.name || '' : '';
      return [
        call.from_number,
        call.to_number,
        call.status,
        call.direction,
        call.disposition || '',
        call.provider,
        isBrowserConversation(call) ? 'browser test voice playground' : 'phone',
        agentName,
        ...languages,
      ].some((value) => value.toLowerCase().includes(query));
    });
  }, [agentMap, calls, deferredQuery, filters.agentId, filters.direction, filters.language, filters.status]);

  const selectedDialAgent = dialForm.agent_id ? agentMap.get(dialForm.agent_id) : undefined;
  const selectedMetadata = callMetadata(selectedCall);
  const selectedRuntime = selectedMetadata.runtime && typeof selectedMetadata.runtime === 'object' && !Array.isArray(selectedMetadata.runtime)
    ? selectedMetadata.runtime as Record<string, unknown>
    : {};
  const metadataLanguages = configuredLanguages(selectedCall);
  const transcriptLanguages = transcript
    ? transcript.turns.map((turn) => transcriptLanguage(turn)).filter(Boolean)
    : [];
  const detectedTranscriptLanguages = Array.from(new Set(transcriptLanguages));
  const version = providerVersion(selectedCall);
  const toolEvents = transcriptToolEvents(transcript);
  return (
    <Layout>
      <div className="page-header">
        <div>
          <span className="page-kicker">Monitor & improve</span>
          <h1>Conversations</h1>
          <p className="page-subtitle">Place controlled outbound calls and inspect recordings, transcripts, configured languages, outcomes, and provider details when the provider reports them.</p>
        </div>
        <div className="header-actions">
          <button type="button" className="btn btn-secondary" disabled={historySyncing} onClick={() => void syncProviderHistory()}><RefreshCw size={14} /> {historySyncing ? 'Syncing history…' : 'Sync provider history'}</button>
          <button type="button" className="btn btn-secondary" disabled={loading} onClick={() => void loadCallsAndAgents()}><RefreshCw size={14} /> Refresh</button>
          <button type="button" className="btn btn-primary" aria-expanded={showDialer} onClick={toggleDialer}>
            {showDialer ? <X size={14} /> : <Plus size={14} />}
            {showDialer ? 'Close' : 'New call'}
          </button>
        </div>
      </div>

      {historySyncNotice ? <div className={styles.recoveryNotice} role="status"><div><strong>Provider history</strong><p>{historySyncNotice}</p></div></div> : null}

      {showDialer ? (
        <form className="card call-dialer" onSubmit={initiateCall}>
          <div className="card-title">
            <div><h2 className={styles.sectionHeading}>Start an outbound call</h2><p>Choose a ready agent and enter one E.164 number. A call starts only after you submit.</p></div>
            <PhoneOutgoing size={18} color="var(--accent)" />
          </div>
          <div className="form-grid">
            <div className="form-group">
              <label htmlFor="dial-agent">Agent</label>
              <select id="dial-agent" name="agent_id" required value={dialForm.agent_id} onChange={(event) => updateDialField('agent_id', event.target.value)}>
                <option value="">Select a published agent</option>
                {agents.filter((agent) => isAgentCallReady(agent, runtimeProfiles[agent.id])).map((agent) => <option value={agent.id} key={agent.id}>{agent.name}</option>)}
              </select>
              {selectedDialAgent ? (
                <p className="form-hint">Configured languages: {Array.from(new Set([selectedDialAgent.language, ...selectedDialAgent.supported_languages])).map(languageName).join(', ')}. This does not certify live switching.</p>
              ) : null}
            </div>
            <div className="form-group">
              <label htmlFor="customer-phone">Customer phone</label>
              <input id="customer-phone" name="tel" type="tel" inputMode="tel" autoComplete="tel" required value={dialForm.to_number} placeholder="+971501234567" pattern="\+[1-9][0-9]{7,14}" onChange={(event) => updateDialField('to_number', event.target.value)} />
              <p className="form-hint">Use country code and number only, for example +971501234567.</p>
            </div>
          </div>
          {dialError ? <div className="inline-error" role="alert">{dialError}</div> : null}
          <button className="btn btn-primary" disabled={dialing}>{dialing ? 'Starting call…' : 'Start outbound call'}</button>
        </form>
      ) : null}

      <section className={`card ${styles.filterCard}`} aria-labelledby="conversation-filter-heading">
        <h2 id="conversation-filter-heading" className="visually-hidden">Filter conversations</h2>
        <div className={styles.filterGrid}>
          <div className={`form-group ${styles.compactGroup}`}>
            <label htmlFor="call-search">Search</label>
            <div className={styles.searchControl}><Search size={14} /><input id="call-search" type="search" placeholder="Number, agent, outcome, provider…" value={filters.query} onChange={(event) => setFilters((current) => ({ ...current, query: event.target.value }))} /></div>
          </div>
          <div className={`form-group ${styles.compactGroup}`}>
            <label htmlFor="call-direction">Direction</label>
            <select id="call-direction" value={filters.direction} onChange={(event) => setFilters((current) => ({ ...current, direction: event.target.value }))}><option value="">All directions</option><option value="inbound">Inbound</option><option value="outbound">Outbound</option></select>
          </div>
          <div className={`form-group ${styles.compactGroup}`}>
            <label htmlFor="call-status">Status</label>
            <select id="call-status" value={filters.status} onChange={(event) => setFilters((current) => ({ ...current, status: event.target.value }))}><option value="">All statuses</option>{availableStatuses.map((status) => <option value={status} key={status}>{status.replace(/_/g, ' ')}</option>)}</select>
          </div>
          <div className={`form-group ${styles.compactGroup}`}>
            <label htmlFor="call-agent">Agent</label>
            <select id="call-agent" value={filters.agentId} onChange={(event) => setFilters((current) => ({ ...current, agentId: event.target.value }))}><option value="">All agents</option>{agents.map((agent) => <option value={agent.id} key={agent.id}>{agent.name}</option>)}</select>
          </div>
          <div className={`form-group ${styles.compactGroup}`}>
            <label htmlFor="call-language">Configured language</label>
            <select id="call-language" value={filters.language} disabled={!availableLanguages.length} onChange={(event) => setFilters((current) => ({ ...current, language: event.target.value }))}><option value="">{availableLanguages.length ? 'All configured languages' : 'No configuration data'}</option>{availableLanguages.map((language) => <option value={language} key={language}>{languageName(language)}</option>)}</select>
          </div>
          <button type="button" className="btn btn-secondary" onClick={() => setFilters(EMPTY_FILTERS)} disabled={Object.values(filters).every((value) => !value)}><FilterX size={13} /> Clear</button>
        </div>
        <div className={styles.filterSummary} aria-live="polite">
          <span>{loading ? 'Loading conversations…' : `${visibleCalls.length} of ${calls.length} loaded conversations`}</span>
          <span>Up to 200 most recent records</span>
        </div>
      </section>

      {loading ? (
        <p className="page-loading" role="status">Loading conversations…</p>
      ) : listError ? (
        <div className={styles.recoveryNotice} role="alert"><div><strong>Conversations could not be loaded</strong><p>{listError}</p></div><button type="button" className="btn btn-secondary btn-sm" onClick={() => void loadCallsAndAgents()}><RefreshCw size={12} /> Retry</button></div>
      ) : calls.length === 0 ? (
        <div className="empty-state"><h3>No conversations yet</h3><p>Phone calls and Voice Playground browser tests will appear here after a session starts.</p></div>
      ) : visibleCalls.length === 0 ? (
        <div className={`empty-state ${styles.resultEmpty}`}><h3>No conversations match</h3><p>Adjust or clear the current search and filters.</p><button type="button" className="btn btn-secondary" onClick={() => setFilters(EMPTY_FILTERS)}>Clear filters</button></div>
      ) : (
        <div className="table-container">
          <table className={styles.callTable}>
            <thead><tr><th>Conversation</th><th>Agent</th><th>Direction</th><th>Status</th><th>Duration</th><th>Outcome</th><th>Configured languages</th><th>Date</th><th>Actions</th></tr></thead>
            <tbody>
              {visibleCalls.map((call) => {
                const languages = configuredLanguages(call);
                const agent = call.agent_id ? agentMap.get(call.agent_id) : undefined;
                const browserConversation = isBrowserConversation(call);
                return (
                  <tr key={call.id}>
                    <td><span className={`${styles.tablePrimary} phone-value`}>{browserConversation ? 'Voice Playground' : call.to_number}</span><span className={styles.tableSecondary}>{browserConversation ? 'Browser test' : `From ${call.from_number}`}</span></td>
                    <td>{agent?.name || (call.agent_id ? 'Deleted or unavailable agent' : 'No agent linked')}</td>
                    <td><span className={`badge ${call.direction === 'inbound' ? 'badge-info' : 'badge-warning'}`}>{call.direction}</span></td>
                    <td><span className={`badge ${statusBadge(call.status)}`}>{call.status.replace(/_/g, ' ')}</span></td>
                    <td>{formatDuration(call.duration_seconds)}</td>
                    <td>{call.disposition || '—'}</td>
                    <td>{languages.length ? languages.map(languageName).join(', ') : 'Not captured'}</td>
                    <td className="table-muted">{formatDate(call.created_at)}</td>
                    <td><button type="button" className="btn btn-secondary btn-sm" onClick={() => openDetails(call)}>Details</button></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {selectedCall ? (
        <div className="call-dialog-layer">
          <button type="button" tabIndex={-1} className="call-dialog-backdrop" aria-label="Close call details" onClick={closeDetails} />
          <section ref={dialogRef} className={`call-dialog ${styles.detailDialog}`} role="dialog" aria-modal="true" aria-labelledby="call-detail-title" aria-describedby="call-detail-description" aria-busy={detailLoading}>
            <header className="call-dialog-header">
              <div>
                <span className="page-kicker">Conversation intelligence</span>
                <h2 id="call-detail-title">Call details</h2>
                <p id="call-detail-description">{isBrowserConversation(selectedCall) ? 'Voice Playground browser test' : `${selectedCall.from_number} to ${selectedCall.to_number}`}</p>
                <div className={styles.detailStatus}>
                  <span className={`badge ${statusBadge(selectedCall.status)}`}>{selectedCall.status.replace(/_/g, ' ')}</span>
                  {metadataLanguages.map((language) => <span className="badge badge-info" key={language}><Globe2 size={10} /> Configured {languageName(language)}</span>)}
                  {detectedTranscriptLanguages.map((language) => <span className="badge badge-success" key={`detected-${language}`}><Globe2 size={10} /> Transcript {languageName(language)}</span>)}
                  {version ? <span className="badge badge-neutral">Version {version}</span> : null}
                </div>
              </div>
              <button ref={closeButtonRef} type="button" className="icon-button" onClick={closeDetails} aria-label="Close call details"><X size={18} /></button>
            </header>

            <div className="call-dialog-content">
              <section className="detail-section" aria-labelledby="call-overview-heading">
                <div className="detail-section-title"><PhoneOutgoing size={16} /><h3 id="call-overview-heading">Overview</h3></div>
                <dl className="call-detail-grid">
                  <div><dt>Status</dt><dd><span className={`badge ${statusBadge(selectedCall.status)}`}>{selectedCall.status.replace(/_/g, ' ')}</span></dd></div>
                  <div><dt>Agent</dt><dd>{selectedCall.agent_id ? agentMap.get(selectedCall.agent_id)?.name || 'Unavailable' : 'Not linked'}</dd></div>
                  <div><dt>Direction</dt><dd>{selectedCall.direction}</dd></div>
                  <div><dt>Duration</dt><dd>{formatDuration(selectedCall.duration_seconds)}</dd></div>
                  <div><dt>Provider</dt><dd>{selectedCall.provider}</dd></div>
                  <div><dt>Channel</dt><dd>{isBrowserConversation(selectedCall) ? 'Browser test' : 'Phone'}</dd></div>
                  <div><dt>Started</dt><dd>{formatDate(selectedCall.started_at)}</dd></div>
                  <div><dt>Answered</dt><dd>{formatDate(selectedCall.answered_at)}</dd></div>
                  <div><dt>Ended</dt><dd>{formatDate(selectedCall.ended_at)}</dd></div>
                  <div><dt>Disposition</dt><dd>{selectedCall.disposition || 'Not reported'}</dd></div>
                  {selectedCall.cost_cents !== null ? <div><dt>Provider cost</dt><dd>{selectedCall.cost_cents} cents</dd></div> : null}
                  {selectedCall.sentiment_score !== null ? <div><dt>Sentiment score</dt><dd>{selectedCall.sentiment_score.toFixed(2)}</dd></div> : null}
                  {typeof selectedMetadata.provider_latency_ms === 'number' ? <div><dt>Provider latency</dt><dd>{selectedMetadata.provider_latency_ms} ms</dd></div> : null}
                  {typeof selectedRuntime.last_speech_end_to_first_audio_ms === 'number' ? <div><dt>Speech end → first audio</dt><dd>{selectedRuntime.last_speech_end_to_first_audio_ms} ms</dd></div> : null}
                  {typeof selectedRuntime.turn_latency_p50_ms === 'number' ? <div><dt>Turn latency p50</dt><dd>{selectedRuntime.turn_latency_p50_ms} ms</dd></div> : null}
                  {typeof selectedRuntime.turn_latency_p95_ms === 'number' ? <div><dt>Turn latency p95</dt><dd>{selectedRuntime.turn_latency_p95_ms} ms</dd></div> : null}
                  {typeof selectedRuntime.last_transcript_to_first_audio_ms === 'number' ? <div><dt>Transcript → first audio</dt><dd>{selectedRuntime.last_transcript_to_first_audio_ms} ms</dd></div> : null}
                  {typeof selectedRuntime.last_llm_first_token_ms === 'number' ? <div><dt>LLM first token</dt><dd>{selectedRuntime.last_llm_first_token_ms} ms</dd></div> : null}
                  {typeof selectedRuntime.last_llm_latency_ms === 'number' ? <div><dt>Last LLM latency</dt><dd>{selectedRuntime.last_llm_latency_ms} ms</dd></div> : null}
                  {typeof selectedRuntime.last_tts_first_byte_ms === 'number' ? <div><dt>Last TTS first byte</dt><dd>{selectedRuntime.last_tts_first_byte_ms} ms</dd></div> : null}
                  {typeof selectedRuntime.llm_tokens === 'number' ? <div><dt>LLM tokens</dt><dd>{selectedRuntime.llm_tokens}</dd></div> : null}
                  {typeof selectedRuntime.barge_in_count === 'number' ? <div><dt>Barge-ins</dt><dd>{selectedRuntime.barge_in_count}</dd></div> : null}
                  {selectedRuntime.cost_state === 'pending_provider_billing_sync' ? <div><dt>Runtime cost</dt><dd>Awaiting provider billing sync</dd></div> : null}
                </dl>
              </section>

              <section className="detail-section" aria-labelledby="call-recording-heading">
                <div className="detail-section-title"><AudioLines size={16} /><h3 id="call-recording-heading">Recording</h3></div>
                {recordingUrl ? (
                  <div>
                    <audio ref={recordingAudioRef} className={styles.recordingPlayer} controls preload="none" src={recordingUrl}>Your browser cannot play this recording.</audio>
                    <p className={styles.recordingNote}>The full bounded recording was loaded through authenticated workspace access. Playback never starts automatically.</p>
                  </div>
                ) : selectedCall.recording_available ? (
                  <div>
                    <button type="button" className="btn btn-secondary btn-sm" disabled={recordingLoading} onClick={() => void loadRecording()}>
                      <AudioLines size={13} /> {recordingLoading ? 'Loading recording…' : recordingError ? 'Retry secure load' : 'Load secure recording'}
                    </button>
                    {recordingLoading ? <p className={styles.recordingNote} role="status">Retrieving one bounded audio file from the provider…</p> : null}
                    {recordingError ? <p className={styles.recordingError} role="alert">{recordingError}</p> : null}
                    {!recordingLoading && !recordingError ? <p className={styles.recordingNote}>Audio is requested only after this action and is never exposed as a provider URL.</p> : null}
                  </div>
                ) : (
                  <p className="detail-empty">No recording is currently reported for this conversation.</p>
                )}
                <p className={styles.recordingPolicy}>Every audio request checks the latest explicit recording revocation for the customer. The absence of a consent record is policy-neutral; it is not treated as either permission or denial.</p>
              </section>

              <section className="detail-section" aria-labelledby="call-summary-heading">
                <div className="detail-section-title"><ListChecks size={16} /><h3 id="call-summary-heading">AI summary</h3>{summary?.sentiment ? <span className="badge badge-info">{summary.sentiment}</span> : null}</div>
                {detailLoading ? (
                  <p className="detail-loading" role="status">Loading summary…</p>
                ) : summary ? (
                  <div className="summary-content" dir="auto">
                    <p>{summary.summary}</p>
                    {summary.key_topics?.length ? <div><h4>Key topics</h4><div className="topic-list">{summary.key_topics.map((topic) => <span className="meta-chip" key={topic}>{topic}</span>)}</div></div> : null}
                    {summary.action_items?.length ? <div><h4>Action items</h4><ol>{summary.action_items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ol></div> : null}
                  </div>
                ) : (
                  <div className="detail-empty" role={summaryError.includes('not available') ? undefined : 'alert'}><p>{summaryError || 'No summary data was returned.'}</p>{summaryError && !summaryError.includes('not available') ? <button type="button" className={`btn btn-secondary btn-sm ${styles.detailRetry}`} onClick={() => void loadDetails(selectedCall.id)}><RefreshCw size={12} /> Retry details</button> : null}</div>
                )}
              </section>

              <section className="detail-section" aria-labelledby="call-transcript-heading">
                <div className="detail-section-title"><FileText size={16} /><h3 id="call-transcript-heading">Transcript</h3></div>
                {detailLoading ? (
                  <p className="detail-loading" role="status">Loading transcript…</p>
                ) : transcript ? (
                  transcript.turns.length ? (
                    <div className="call-transcript">
                      {transcript.turns.map((turn, index) => {
                        const speaker = valueFromRecord(turn, ['role', 'speaker', 'actor']) || `Turn ${index + 1}`;
                        const content = valueFromRecord(turn, ['text', 'content', 'transcript', 'message']) || 'No text captured.';
                        const language = transcriptLanguage(turn);
                        return <div className="call-transcript-turn" key={`${index}-${speaker}`} lang={language || undefined} dir="auto"><div className={styles.transcriptMeta}><strong>{speaker}</strong><span>{language ? languageName(language) : 'Language not reported'}</span></div><p>{content}</p></div>;
                      })}
                    </div>
                  ) : (
                    <p className="call-transcript-full" dir="auto">{transcript.full_text || 'No spoken text was captured.'}</p>
                  )
                ) : (
                  <div className="detail-empty" role={transcriptError.includes('not available') ? undefined : 'alert'}><p>{transcriptError || 'No transcript data was returned.'}</p>{transcriptError && !transcriptError.includes('not available') ? <button type="button" className={`btn btn-secondary btn-sm ${styles.detailRetry}`} onClick={() => void loadDetails(selectedCall.id)}><RefreshCw size={12} /> Retry details</button> : null}</div>
                )}
              </section>

              {toolEvents.length ? (
                <section className="detail-section" aria-labelledby="call-tools-heading">
                  <div className="detail-section-title"><Wrench size={16} /><h3 id="call-tools-heading">Tool events</h3><span className="badge badge-neutral">{toolEvents.length}</span></div>
                  <div className={styles.toolEvents}>{toolEvents.map((event) => <div className={styles.toolEvent} key={event.id}><strong>{event.name}</strong><span>{event.status.replace(/_/g, ' ')}</span></div>)}</div>
                </section>
              ) : null}
            </div>
          </section>
        </div>
      ) : null}
    </Layout>
  );
}
