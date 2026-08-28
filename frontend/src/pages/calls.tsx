import { useCallback, useEffect, useRef, useState } from 'react';
import { FileText, ListChecks, PhoneOutgoing, Plus, X } from 'lucide-react';
import Layout from '@/components/Layout';
import {
  api,
  CallRecord,
  CallSummary,
  CallTranscript,
  VoiceAgent,
} from '@/lib/api';

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

function formatDate(value: string | null) {
  return value ? new Date(value).toLocaleString() : '—';
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
    ? `${subject} is not available yet.`
    : message;
}

function turnValue(turn: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = turn[key];
    if (typeof value === 'string' && value.trim()) return value;
  }
  return '';
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

export default function Calls() {
  const [calls, setCalls] = useState<CallRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState('');
  const [selectedCall, setSelectedCall] = useState<CallRecord | null>(null);
  const [transcript, setTranscript] = useState<CallTranscript | null>(null);
  const [summary, setSummary] = useState<CallSummary | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [transcriptError, setTranscriptError] = useState('');
  const [summaryError, setSummaryError] = useState('');
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [showDialer, setShowDialer] = useState(false);
  const [dialing, setDialing] = useState(false);
  const [dialError, setDialError] = useState('');
  const [dialForm, setDialForm] = useState({ agent_id: '', to_number: '' });
  const dialIntentKeyRef = useRef<string | null>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const lastFocusedRef = useRef<HTMLElement | null>(null);

  const openDetails = (call: CallRecord) => {
    setTranscript(null);
    setSummary(null);
    setTranscriptError('');
    setSummaryError('');
    setDetailLoading(true);
    setSelectedCall(call);
  };

  useEffect(() => {
    let active = true;
    Promise.allSettled([api.listCalls(), api.listAgents()]).then(([callsResult, agentsResult]) => {
      if (!active) return;
      if (callsResult.status === 'fulfilled') {
        setCalls(callsResult.value);
      } else {
        setListError(callsResult.reason instanceof Error ? callsResult.reason.message : 'Could not load calls.');
      }
      if (agentsResult.status === 'fulfilled') setAgents(agentsResult.value);
      setLoading(false);
    });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!selectedCall) return;

    let active = true;
    Promise.allSettled([
      api.getCallTranscript(selectedCall.id),
      api.getCallSummary(selectedCall.id),
    ]).then(([transcriptResult, summaryResult]) => {
      if (!active) return;
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
    });

    return () => {
      active = false;
    };
  }, [selectedCall]);

  const closeDetails = useCallback(() => setSelectedCall(null), []);

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
        'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
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
      setCalls((current) => [call, ...current]);
      setDialForm({ agent_id: '', to_number: '' });
      setShowDialer(false);
    } catch (error) {
      setDialError(error instanceof Error ? error.message : 'Could not start the call.');
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

  const filterCalls = async (direction: string) => {
    setLoading(true);
    setListError('');
    try {
      setCalls(await api.listCalls(direction ? { direction } : {}));
    } catch (error) {
      setListError(error instanceof Error ? error.message : 'Could not load calls.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Layout>
      <div className="page-header">
        <div>
          <span className="page-kicker">Monitor & improve</span>
          <h1>Conversations</h1>
          <p className="page-subtitle">Place controlled outbound calls and review every transcript, outcome, and provider insight.</p>
        </div>
        <div className="header-actions">
          <label className="visually-hidden" htmlFor="call-direction">Filter by direction</label>
          <select id="call-direction" className="field-control" onChange={(event) => void filterCalls(event.target.value)}>
            <option value="">All directions</option>
            <option value="inbound">Inbound</option>
            <option value="outbound">Outbound</option>
          </select>
          <button
            type="button"
            className="btn btn-primary"
            aria-expanded={showDialer}
            onClick={toggleDialer}
          >
            {showDialer ? <X size={14} /> : <Plus size={14} />}
            {showDialer ? 'Close' : 'New call'}
          </button>
        </div>
      </div>

      {showDialer ? (
        <form className="card call-dialer" onSubmit={initiateCall}>
          <div className="card-title">
            <div><h3>Start a Smallest.ai outbound call</h3><p>Use E.164 phone format. The provider conversation ID is stored automatically.</p></div>
            <PhoneOutgoing size={18} color="var(--accent)" />
          </div>
          <div className="form-grid">
            <div className="form-group">
              <label htmlFor="dial-agent">Agent</label>
              <select
                id="dial-agent"
                name="agent_id"
                required
                value={dialForm.agent_id}
                onChange={(event) => updateDialField('agent_id', event.target.value)}
              >
                <option value="">Select a provisioned agent</option>
                {agents.filter((agent) => agent.provider_agent_id).map((agent) => (
                  <option value={agent.id} key={agent.id}>{agent.name}</option>
                ))}
              </select>
            </div>
            <div className="form-group">
              <label htmlFor="customer-phone">Customer phone</label>
              <input
                id="customer-phone"
                name="tel"
                type="tel"
                inputMode="tel"
                autoComplete="tel"
                required
                value={dialForm.to_number}
                placeholder="+971501234567"
                onChange={(event) => updateDialField('to_number', event.target.value)}
              />
            </div>
          </div>
          {dialError ? <div className="inline-error" role="alert">{dialError}</div> : null}
          <button className="btn btn-primary" disabled={dialing}>{dialing ? 'Starting…' : 'Start outbound call'}</button>
        </form>
      ) : null}

      {loading ? (
        <p className="page-loading" role="status">Loading calls…</p>
      ) : listError ? (
        <div className="inline-error inline-error-card" role="alert">{listError}</div>
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
                  <td><span className={`badge ${call.direction === 'inbound' ? 'badge-info' : 'badge-warning'}`}>{call.direction}</span></td>
                  <td className="phone-value">{call.from_number}</td>
                  <td className="phone-value">{call.to_number}</td>
                  <td><span className={`badge ${statusBadge(call.status)}`}>{call.status.replace(/_/g, ' ')}</span></td>
                  <td>{formatDuration(call.duration_seconds)}</td>
                  <td>{call.disposition || '—'}</td>
                  <td className="table-muted">{formatDate(call.created_at)}</td>
                  <td>
                    <button type="button" className="btn btn-secondary btn-sm" onClick={() => openDetails(call)}>Details</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selectedCall ? (
        <div className="call-dialog-layer">
          <button type="button" tabIndex={-1} className="call-dialog-backdrop" aria-label="Close call details" onClick={closeDetails} />
          <section
            ref={dialogRef}
            className="call-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="call-detail-title"
            aria-describedby="call-detail-description"
          >
            <header className="call-dialog-header">
              <div>
                <span className="page-kicker">Conversation intelligence</span>
                <h2 id="call-detail-title">Call details</h2>
                <p id="call-detail-description">{selectedCall.from_number} to {selectedCall.to_number}</p>
              </div>
              <button ref={closeButtonRef} type="button" className="icon-button" onClick={closeDetails} aria-label="Close call details"><X size={18} /></button>
            </header>

            <div className="call-dialog-content">
              <section className="detail-section" aria-labelledby="call-overview-heading">
                <div className="detail-section-title">
                  <PhoneOutgoing size={16} />
                  <h3 id="call-overview-heading">Overview</h3>
                </div>
                <dl className="call-detail-grid">
                  <div><dt>Status</dt><dd><span className={`badge ${statusBadge(selectedCall.status)}`}>{selectedCall.status.replace(/_/g, ' ')}</span></dd></div>
                  <div><dt>Direction</dt><dd>{selectedCall.direction}</dd></div>
                  <div><dt>Duration</dt><dd>{formatDuration(selectedCall.duration_seconds)}</dd></div>
                  <div><dt>Provider</dt><dd>{selectedCall.provider}</dd></div>
                  <div><dt>Started</dt><dd>{formatDate(selectedCall.started_at)}</dd></div>
                  <div><dt>Answered</dt><dd>{formatDate(selectedCall.answered_at)}</dd></div>
                  <div><dt>Ended</dt><dd>{formatDate(selectedCall.ended_at)}</dd></div>
                  <div><dt>Disposition</dt><dd>{selectedCall.disposition || '—'}</dd></div>
                </dl>
              </section>

              <section className="detail-section" aria-labelledby="call-summary-heading">
                <div className="detail-section-title">
                  <ListChecks size={16} />
                  <h3 id="call-summary-heading">AI summary</h3>
                  {summary?.sentiment ? <span className="badge badge-info">{summary.sentiment}</span> : null}
                </div>
                {detailLoading ? (
                  <p className="detail-loading" role="status">Loading summary…</p>
                ) : summary ? (
                  <div className="summary-content">
                    <p>{summary.summary}</p>
                    {summary.key_topics?.length ? (
                      <div><h4>Key topics</h4><div className="topic-list">{summary.key_topics.map((topic) => <span className="meta-chip" key={topic}>{topic}</span>)}</div></div>
                    ) : null}
                    {summary.action_items?.length ? (
                      <div><h4>Action items</h4><ol>{summary.action_items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ol></div>
                    ) : null}
                  </div>
                ) : (
                  <p className="detail-empty" role={summaryError.includes('not available') ? undefined : 'alert'}>{summaryError || 'No summary data.'}</p>
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
                        const speaker = turnValue(turn, ['role', 'speaker', 'actor']) || `Turn ${index + 1}`;
                        const content = turnValue(turn, ['text', 'content', 'transcript', 'message']) || 'No text captured.';
                        return <div className="call-transcript-turn" key={index}><strong>{speaker}</strong><p>{content}</p></div>;
                      })}
                    </div>
                  ) : (
                    <p className="call-transcript-full">{transcript.full_text || 'No spoken text was captured.'}</p>
                  )
                ) : (
                  <p className="detail-empty" role={transcriptError.includes('not available') ? undefined : 'alert'}>{transcriptError || 'No transcript data.'}</p>
                )}
              </section>
            </div>
          </section>
        </div>
      ) : null}
    </Layout>
  );
}
