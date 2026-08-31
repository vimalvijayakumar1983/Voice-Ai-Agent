import { useRouter } from 'next/router';
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  FlaskConical,
  Gauge,
  Globe2,
  MessageSquareText,
  Mic,
  MicOff,
  PhoneCall,
  PhoneOff,
  Play,
  RefreshCw,
  ShieldCheck,
  TimerReset,
  Volume2,
} from 'lucide-react';
import Layout from '@/components/Layout';
import { agentTestReadinessMessage, isAgentCallReady } from '@/lib/agent-readiness.cjs';
import { parseTestVariables, reduceTranscriptState, sessionErrorGuidance } from '@/lib/conversation-ui.cjs';
import { api, RuntimeProfile, VoiceAgent } from '@/lib/api';
import styles from '@/styles/conversation-operations.module.css';
import type { AtomsAgent } from '@smallest-ai/agent-sdk';

type SessionState = 'idle' | 'connecting' | 'listening' | 'speaking' | 'ended' | 'error';
type TestScenario = 'standard' | 'language_switch' | 'interruption';
type ErrorNotice = { title: string; message: string };
type TokenState = 'not requested' | 'requesting' | 'issued' | 'consumed' | 'failed';
type Diagnostics = {
  permission: 'not requested' | 'granted' | 'blocked';
  token: TokenState;
  tokenLifetimeSeconds: number | null;
  sampleRate: number | null;
  connectTimeMs: number | null;
  sessionId: string;
  callId: string;
  eventCount: number;
  lastEvent: string;
  endReason: string;
};

const INITIAL_DIAGNOSTICS: Diagnostics = {
  permission: 'not requested',
  token: 'not requested',
  tokenLifetimeSeconds: null,
  sampleRate: null,
  connectTimeMs: null,
  sessionId: '',
  callId: '',
  eventCount: 0,
  lastEvent: 'No session events',
  endReason: '',
};

const CONNECT_TIMEOUT_MS = 12_000;

function languageName(code: string) {
  try {
    return new Intl.DisplayNames(['en'], { type: 'language' }).of(code) || code.toUpperCase();
  } catch {
    return code.toUpperCase();
  }
}

function shortIdentifier(value: string) {
  if (!value) return '—';
  return value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-5)}` : value;
}

async function requestMicrophoneReadiness() {
  if (!globalThis.isSecureContext) {
    throw new DOMException('Microphone access requires a secure context.', 'SecurityError');
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new DOMException('No browser microphone interface is available.', 'NotFoundError');
  }
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  stream.getTracks().forEach((track) => track.stop());
}

export default function Playground() {
  const router = useRouter();
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [runtimeProfiles, setRuntimeProfiles] = useState<Record<string, RuntimeProfile>>({});
  const [agentsLoading, setAgentsLoading] = useState(true);
  const [agentsError, setAgentsError] = useState('');
  const [selectedId, setSelectedId] = useState('');
  const [state, setState] = useState<SessionState>('idle');
  const [transcriptState, dispatchTranscript] = useReducer(reduceTranscriptState, { turns: [], live: null });
  const [muted, setMuted] = useState(false);
  const [variablesText, setVariablesText] = useState('{\n  "customer_name": "Vimal"\n}');
  const [error, setError] = useState<ErrorNotice | null>(null);
  const [scenario, setScenario] = useState<TestScenario>('standard');
  const [startLanguage, setStartLanguage] = useState('');
  const [switchLanguage, setSwitchLanguage] = useState('');
  const [diagnostics, setDiagnostics] = useState<Diagnostics>(INITIAL_DIAGNOSTICS);
  const agentRef = useRef<AtomsAgent | null>(null);
  const attemptRef = useRef(false);
  const terminalStateRef = useRef<'ended' | 'error' | null>(null);
  const turnIdRef = useRef(0);
  const connectStartedAtRef = useRef(0);
  const transcriptPanelRef = useRef<HTMLDivElement>(null);
  const transcript = transcriptState.turns;
  const liveTranscript = transcriptState.live;

  const loadAgents = useCallback(async () => {
    setAgentsLoading(true);
    setAgentsError('');
    try {
      const [items, profiles] = await Promise.all([
        api.listAgents(),
        api.listRuntimeProfiles(),
      ]);
      setAgents(items);
      setRuntimeProfiles(Object.fromEntries(profiles.map((profile) => [profile.agent_id, profile])));
      const requested = typeof router.query.agent === 'string' ? router.query.agent : '';
      setSelectedId((current) => {
        if (requested && items.some((agent) => agent.id === requested)) return requested;
        if (current && items.some((agent) => agent.id === current)) return current;
        return items[0]?.id || '';
      });
    } catch (loadError) {
      setAgents([]);
      setRuntimeProfiles({});
      setAgentsError(loadError instanceof Error ? loadError.message : 'Could not load voice agents.');
    } finally {
      setAgentsLoading(false);
    }
  }, [router.query.agent]);

  useEffect(() => {
    void loadAgents();
  }, [loadAgents]);

  useEffect(() => () => agentRef.current?.disconnect(), []);

  const selected = useMemo(
    () => agents.find((agent) => agent.id === selectedId),
    [agents, selectedId],
  );
  const selectedRuntimeProfile = selected ? runtimeProfiles[selected.id] : undefined;
  const selectedReady = isAgentCallReady(selected, selectedRuntimeProfile);
  const selectedIsVav = ['sarvam', 'elevenlabs'].includes(selected?.voice_provider ?? '');
  const selectedPhoneNumber = selectedRuntimeProfile?.assigned_numbers[0] || '';
  const browserTestReady = selectedReady && !selectedIsVav;
  const active = state === 'connecting' || state === 'listening' || state === 'speaking';
  const selectedLanguages = useMemo(() => {
    if (!selected) return [];
    return Array.from(new Set([selected.language, ...(selected.supported_languages || [])].filter(Boolean)));
  }, [selected]);
  const multilingualConfigured = selectedLanguages.length > 1;

  useEffect(() => {
    const primary = selected?.language || selectedLanguages[0] || '';
    const alternative = selectedLanguages.find((language) => language !== primary) || '';
    setStartLanguage(primary);
    setSwitchLanguage(alternative);
    setScenario((current) => current === 'language_switch' && !alternative ? 'standard' : current);
    dispatchTranscript({ type: 'clear' });
    setError(null);
    setState('idle');
    setDiagnostics(INITIAL_DIAGNOSTICS);
  }, [selected?.id, selected?.language, selectedLanguages]);

  useEffect(() => {
    transcriptPanelRef.current?.scrollTo({
      top: transcriptPanelRef.current.scrollHeight,
      behavior: 'smooth',
    });
  }, [liveTranscript, transcript]);

  const recordEvent = useCallback((name: string) => {
    setDiagnostics((current) => ({
      ...current,
      eventCount: current.eventCount + 1,
      lastEvent: name,
    }));
  }, []);

  const startSession = async () => {
    if (attemptRef.current) return;
    if (!selected || !isAgentCallReady(selected, selectedRuntimeProfile) || ['sarvam', 'elevenlabs'].includes(selected.voice_provider)) {
      setError({
        title: 'Agent is not ready to test',
        message: ['sarvam', 'elevenlabs'].includes(selected?.voice_provider ?? '')
          ? 'This VAV realtime agent is tested through its assigned phone number.'
          : selected ? agentTestReadinessMessage(selected, selectedRuntimeProfile) : 'Select a voice agent before testing.',
      });
      return;
    }

    let variables: Record<string, string | number | boolean>;
    try {
      variables = parseTestVariables(variablesText);
    } catch (variablesError) {
      setError({
        title: 'Check pre-call variables',
        message: variablesError instanceof Error ? variablesError.message : 'Pre-call variables are invalid.',
      });
      return;
    }

    attemptRef.current = true;
    terminalStateRef.current = null;
    agentRef.current?.disconnect();
    agentRef.current = null;
    setState('connecting');
    setError(null);
    dispatchTranscript({ type: 'clear' });
    setMuted(false);
    setDiagnostics({ ...INITIAL_DIAGNOSTICS, lastEvent: 'Preparing microphone' });

    try {
      // Permission and the lazy SDK download happen before token issuance so a
      // 30-second single-use credential is never spent waiting on either.
      await requestMicrophoneReadiness();
      setDiagnostics((current) => ({ ...current, permission: 'granted', lastEvent: 'Microphone ready' }));
      const { AtomsAgent } = await import('@smallest-ai/agent-sdk');

      setDiagnostics((current) => ({ ...current, token: 'requesting', lastEvent: 'Requesting secure token' }));
      const session = await api.createSmallestSession(selected.id, variables);
      setDiagnostics((current) => ({
        ...current,
        token: 'issued',
        tokenLifetimeSeconds: session.expires_in,
        sampleRate: session.sample_rate,
        lastEvent: 'Single-use token issued',
      }));

      const voiceAgent = new AtomsAgent({
        apiKey: session.access_token,
        agentId: selected.provider_agent_id || '',
        sampleRate: session.sample_rate,
      });
      agentRef.current = voiceAgent;
      connectStartedAtRef.current = performance.now();

      voiceAgent.on('session_started', (event) => {
        terminalStateRef.current = null;
        setState('listening');
        setDiagnostics((current) => ({
          ...current,
          token: 'consumed',
          connectTimeMs: Math.round(performance.now() - connectStartedAtRef.current),
          sessionId: event.session_id,
          callId: event.call_id,
          eventCount: current.eventCount + 1,
          lastEvent: 'Session started',
        }));
        void api.registerBrowserConversation(selected.id, event.call_id).catch((registrationError) => {
          setDiagnostics((current) => ({
            ...current,
            lastEvent: registrationError instanceof Error
              ? `Session started; history registration failed: ${registrationError.message}`
              : 'Session started; history registration failed',
          }));
        });
      });
      voiceAgent.on('agent_start_talking', () => {
        setState('speaking');
        recordEvent('Agent started speaking');
      });
      voiceAgent.on('agent_stop_talking', () => {
        setState('listening');
        recordEvent('Agent stopped speaking');
      });
      voiceAgent.on('transcript_delta', (event) => {
        if (!event.text.trim()) return;
        dispatchTranscript({ type: 'delta', role: event.role, text: event.text });
        recordEvent(`${event.role === 'assistant' ? 'Agent' : 'User'} transcript update`);
      });
      voiceAgent.on('transcript', (event) => {
        const text = event.text.trim();
        if (!text) return;
        turnIdRef.current += 1;
        dispatchTranscript({ type: 'settled', id: turnIdRef.current, role: event.role, text });
        recordEvent(`${event.role === 'assistant' ? 'Agent' : 'User'} turn settled`);
      });
      voiceAgent.on('session_ended', (event) => {
        agentRef.current = null;
        setMuted(false);
        dispatchTranscript({ type: 'clear_live' });
        setDiagnostics((current) => ({
          ...current,
          eventCount: current.eventCount + 1,
          lastEvent: 'Session ended',
          endReason: event.reason || 'Provider ended the session',
        }));
        if (terminalStateRef.current !== 'error') setState('ended');
      });
      voiceAgent.on('error', (event) => {
        terminalStateRef.current = 'error';
        setError(sessionErrorGuidance(new Error(event.message || event.code)));
        setState('error');
        setDiagnostics((current) => ({
          ...current,
          token: current.token === 'requesting' ? 'failed' : current.token,
          eventCount: current.eventCount + 1,
          lastEvent: event.code ? `Provider error: ${event.code}` : 'Provider error',
        }));
        voiceAgent.disconnect();
      });

      let timeoutId: ReturnType<typeof setTimeout> | undefined;
      const timeout = new Promise<never>((_, reject) => {
        timeoutId = setTimeout(
          () => reject(new Error('WebSocket connection timed out.')),
          CONNECT_TIMEOUT_MS,
        );
      });
      try {
        await Promise.race([voiceAgent.connect(), timeout]);
      } finally {
        if (timeoutId) clearTimeout(timeoutId);
      }
    } catch (sessionError) {
      terminalStateRef.current = 'error';
      agentRef.current?.disconnect();
      agentRef.current = null;
      setError(sessionErrorGuidance(sessionError));
      setState('error');
      setDiagnostics((current) => ({
        ...current,
        permission: sessionError instanceof DOMException && (
          sessionError.name === 'NotAllowedError'
          || sessionError.name === 'NotFoundError'
          || sessionError.name === 'NotReadableError'
        ) ? 'blocked' : current.permission,
        token: current.token === 'requesting' || current.token === 'issued' ? 'failed' : current.token,
        lastEvent: 'Connection failed',
      }));
    } finally {
      attemptRef.current = false;
    }
  };

  const endSession = () => {
    terminalStateRef.current = 'ended';
    agentRef.current?.disconnect();
    agentRef.current = null;
    dispatchTranscript({ type: 'clear_live' });
    setState('ended');
    setMuted(false);
  };

  const toggleMute = () => {
    if (!agentRef.current) return;
    if (muted) agentRef.current.unmute(); else agentRef.current.mute();
    setMuted((value) => !value);
    recordEvent(muted ? 'Microphone unmuted' : 'Microphone muted');
  };

  const switchTargetOptions = selectedLanguages.filter((language) => language !== startLanguage);
  const scenarioInstruction = scenario === 'language_switch'
    ? `Begin in ${languageName(startLanguage)}, ask the agent to continue in ${languageName(switchLanguage)}, then return to ${languageName(startLanguage)} without repeating the customer context.`
    : scenario === 'interruption'
      ? 'Interrupt the agent while it is speaking, then confirm it stops cleanly and retains the request.'
      : 'Run a normal conversation and verify the greeting, turn-taking, response quality, and closing.';

  return (
    <Layout>
      <div className="page-header">
        <div>
          <span className="page-kicker">Safe test environment</span>
          <h1>Voice playground</h1>
          <p className="page-subtitle">Test each deployed voice agent through its supported channel: a secure browser session or its assigned phone number.</p>
        </div>
        <span className="badge badge-success"><ShieldCheck size={12} /> Secure token flow</span>
      </div>

      <div className="playground-layout">
        <section className="card playground-config" aria-labelledby="test-config-heading">
          <div className="card-title">
            <div><h2 id="test-config-heading" className={styles.sectionHeading}>Test configuration</h2><p>Choose an agent, scenario, and optional call context.</p></div>
            <FlaskConical size={17} color="var(--accent)" />
          </div>

          {agentsError ? (
            <div className={styles.recoveryNotice} role="alert">
              <div><strong>Voice agents could not be loaded</strong><p>{agentsError}</p></div>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => void loadAgents()}><RefreshCw size={12} /> Retry</button>
            </div>
          ) : null}

          <div className="form-group">
            <label htmlFor="playground-agent">Voice agent</label>
            <select
              id="playground-agent"
              value={selectedId}
              disabled={active || agentsLoading}
              onChange={(event) => setSelectedId(event.target.value)}
            >
              <option value="">{agentsLoading ? 'Loading agents…' : 'Select an agent'}</option>
              {agents.map((agent) => {
                const ready = isAgentCallReady(agent, runtimeProfiles[agent.id]);
                const status = ['sarvam', 'elevenlabs'].includes(agent.voice_provider) && ready ? ' — phone ready' : ready ? '' : ' — not ready';
                return <option value={agent.id} key={agent.id}>{agent.name}{status}</option>;
              })}
            </select>
          </div>

          {selected ? (
            <div className="provider-alert">
              <Bot size={15} aria-hidden="true" />
              <div>
                <strong>{selected.name}</strong>
                <p>{selectedReady
                  ? selectedIsVav
                    ? `${selected.voice_provider === 'elevenlabs' ? 'ElevenLabs voice' : 'Sarvam AI'} phone runtime active${selectedPhoneNumber ? ` · ${selectedPhoneNumber}` : ''}`
                    : `Smallest.ai · ${languageName(selected.language)} · ${selected.model_name}`
                  : agentTestReadinessMessage(selected, selectedRuntimeProfile)}</p>
              </div>
            </div>
          ) : null}

          {selected ? (
            <section className={styles.languageReadiness} aria-labelledby="language-readiness-heading">
              <div className={styles.inlineHeading}>
                <Globe2 size={15} aria-hidden="true" />
                <div>
                  <h3 id="language-readiness-heading">Language readiness</h3>
                  <p>{multilingualConfigured
                    ? `${selectedLanguages.length} languages are selected. Same-call switching is not proven until this scenario passes.`
                    : 'This agent is configured for one language; same-call switching cannot be tested.'}</p>
                </div>
              </div>
              <div className={styles.languageChips} aria-label="Configured languages">
                {selectedLanguages.map((language) => (
                  <span className="meta-chip" key={language}>{languageName(language)}{language === selected.language ? ' · Primary' : ''}</span>
                ))}
              </div>
              <p className={styles.honestNote}><AlertTriangle size={13} aria-hidden="true" /> Configuration alone does not confirm provider switching or voice pronunciation quality.</p>
            </section>
          ) : null}

          <div className="form-group">
            <label htmlFor="test-scenario">Test scenario</label>
            <select
              id="test-scenario"
              value={scenario}
              disabled={active}
              onChange={(event) => setScenario(event.target.value as TestScenario)}
            >
              <option value="standard">Standard conversation</option>
              <option value="language_switch" disabled={!multilingualConfigured}>Same-call language switch</option>
              <option value="interruption">Interruption and recovery</option>
            </select>
          </div>

          {scenario === 'language_switch' && multilingualConfigured ? (
            <div className={styles.scenarioGrid}>
              <div className="form-group">
                <label htmlFor="scenario-start-language">Start language</label>
                <select id="scenario-start-language" value={startLanguage} disabled={active} onChange={(event) => {
                  const nextStart = event.target.value;
                  setStartLanguage(nextStart);
                  if (switchLanguage === nextStart) setSwitchLanguage(selectedLanguages.find((language) => language !== nextStart) || '');
                }}>
                  {selectedLanguages.map((language) => <option value={language} key={language}>{languageName(language)}</option>)}
                </select>
              </div>
              <div className="form-group">
                <label htmlFor="scenario-switch-language">Switch to</label>
                <select id="scenario-switch-language" value={switchLanguage} disabled={active} onChange={(event) => setSwitchLanguage(event.target.value)}>
                  {switchTargetOptions.map((language) => <option value={language} key={language}>{languageName(language)}</option>)}
                </select>
              </div>
            </div>
          ) : null}

          <div className={styles.scenarioBrief}>
            <strong>Pass condition</strong>
            <p>{scenarioInstruction}</p>
          </div>

          <div className="form-group">
            <label htmlFor="pre-call-variables">Pre-call variables <span>JSON, scalar values only</span></label>
            <textarea
              id="pre-call-variables"
              value={variablesText}
              disabled={active}
              spellCheck={false}
              onChange={(event) => {
                setVariablesText(event.target.value);
                if (error?.title === 'Check pre-call variables') setError(null);
              }}
            />
            <p className="form-hint">Values fill matching prompt variables for this session only. Objects and arrays are rejected before microphone access.</p>
          </div>

          <div className="card" style={{ background: 'var(--bg-muted)', boxShadow: 'none' }}>
            <div className="activity-list">
              <div className="activity-item"><div className="activity-icon"><Mic size={14} /></div><div><strong>Microphone first</strong><p>Permission is confirmed before requesting a token</p></div></div>
              <div className="activity-item"><div className="activity-icon"><Volume2 size={14} /></div><div><strong>Live audio output</strong><p>Provider sample rate is shown in diagnostics</p></div></div>
              <div className="activity-item"><div className="activity-icon"><MessageSquareText size={14} /></div><div><strong>Settled transcript</strong><p>Cumulative deltas update one live turn instead of duplicating text</p></div></div>
            </div>
          </div>
        </section>

        <section className="card voice-stage" aria-labelledby="voice-stage-heading">
          <div className="voice-stage-header">
            <div><h2 id="voice-stage-heading" className={styles.stageHeading}>{selected?.name || 'Select an agent'}</h2><p>{selectedIsVav ? `${selected?.voice_provider === 'elevenlabs' ? 'ElevenLabs' : 'Sarvam AI'} phone session` : 'Smallest.ai Atoms browser session'}</p></div>
            <span className={`badge ${selectedIsVav && selectedReady ? 'badge-success' : active ? 'badge-success' : state === 'error' ? 'badge-danger' : 'badge-neutral'}`}>{selectedIsVav ? selectedReady ? 'active' : 'not ready' : state}</span>
          </div>

          <div className={styles.stageLanguageRow} aria-label="Test language configuration">
            {selectedLanguages.map((language) => <span key={language}>{languageName(language)}</span>)}
            {selected ? <small>{multilingualConfigured ? 'Switching requires a live pass' : 'Single-language configuration'}</small> : null}
          </div>

          <div className="voice-orb-wrap">
            <div>
              <div className={`voice-orb ${state === 'listening' || state === 'speaking' ? 'listening' : ''}`} aria-hidden="true" />
              <div className="session-status" style={{ marginTop: 28 }} aria-live="polite" aria-atomic="true">
                <strong>{selectedIsVav && selectedReady ? 'Phone runtime ready' : sessionLabel(state)}</strong>
                <span>{selectedIsVav && selectedReady
                  ? `Call ${selectedPhoneNumber} from any phone to test Customer Support.`
                  : sessionDescription(state, selected, selectedReady, selectedRuntimeProfile)}</span>
              </div>
            </div>
          </div>

          {selectedIsVav ? (
            <>
              <div className={styles.diagnostics} aria-label="Phone runtime diagnostics">
                <div><PhoneCall size={13} /><span>Phone number</span><strong>{selectedPhoneNumber || 'Not assigned'}</strong></div>
                <div><ShieldCheck size={13} /><span>Runtime</span><strong>{selectedRuntimeProfile?.status || 'not loaded'}</strong></div>
                <div><CheckCircle2 size={13} /><span>Readiness</span><strong>{selectedReady ? 'all gates passed' : 'attention required'}</strong></div>
                <div><Gauge size={13} /><span>Call limit</span><strong>{selectedRuntimeProfile?.daily_call_limit ?? '—'} / day</strong></div>
              </div>
              <div className={styles.diagnosticLine} aria-live="polite">
                <span>Twilio Media Streams</span>
                <span>{selected?.voice_provider === 'elevenlabs' ? 'ElevenLabs speech · Sarvam transcription' : 'Sarvam speech'}</span>
                <span>OpenAI response engine</span>
                <span>Completed calls appear in Conversations</span>
              </div>
              <div className={`transcript-panel ${styles.transcriptPanel}`}>
                <div className="transcript-empty"><div><PhoneCall size={20} style={{ marginBottom: 8 }} /><br />Use a phone to place a real inbound test call. Review the completed transcript in Conversations.</div></div>
              </div>
            </>
          ) : (
            <>
              <div className={styles.diagnostics} aria-label="Session diagnostics">
                <div><Mic size={13} /><span>Microphone</span><strong>{diagnostics.permission}</strong></div>
                <div><TimerReset size={13} /><span>Secure token</span><strong>{diagnostics.token}</strong></div>
                <div><Gauge size={13} /><span>Connect time</span><strong>{diagnostics.connectTimeMs === null ? '—' : `${diagnostics.connectTimeMs} ms`}</strong></div>
                <div title={diagnostics.sessionId || undefined}><CheckCircle2 size={13} /><span>Session</span><strong>{shortIdentifier(diagnostics.sessionId)}</strong></div>
              </div>

              <div className={styles.diagnosticLine} aria-live="polite">
                <span>{diagnostics.sampleRate ? `${(diagnostics.sampleRate / 1000).toFixed(0)} kHz` : 'Sample rate pending'}</span>
                {diagnostics.tokenLifetimeSeconds ? <span>{diagnostics.tokenLifetimeSeconds}s single-use token</span> : null}
                <span>{diagnostics.eventCount} events</span>
                <span>{diagnostics.lastEvent}</span>
                {diagnostics.callId ? <span title={diagnostics.callId}>Call {shortIdentifier(diagnostics.callId)}</span> : null}
                {diagnostics.endReason ? <span>Ended: {diagnostics.endReason}</span> : null}
              </div>

              <div
                ref={transcriptPanelRef}
                className={`transcript-panel ${styles.transcriptPanel}`}
                role="log"
                aria-live="polite"
                aria-relevant="additions text"
                aria-label="Live conversation transcript"
              >
                {transcript.length === 0 && !liveTranscript ? (
                  <div className="transcript-empty"><div><MessageSquareText size={20} style={{ marginBottom: 8 }} /><br />Live transcript will appear here after the first turn.</div></div>
                ) : (
                  <>
                    {transcript.map((turn) => (
                      <div className={`transcript-turn ${turn.role}`} key={turn.id} dir="auto">
                        <small>{turn.role} · language not reported by SDK</small>
                        {turn.text}
                      </div>
                    ))}
                    {liveTranscript ? (
                      <div className={`transcript-turn ${liveTranscript.role} ${styles.liveTurn}`} dir="auto">
                        <small>{liveTranscript.role} · live · language not reported by SDK</small>
                        {liveTranscript.text}
                      </div>
                    ) : null}
                  </>
                )}
              </div>
            </>
          )}

          {error ? (
            <div className={styles.stageError} role="alert">
              <AlertTriangle size={16} aria-hidden="true" />
              <div><strong>{error.title}</strong><p>{error.message}</p></div>
              <button type="button" className={styles.stageRetry} disabled={!browserTestReady || active} onClick={() => void startSession()}><RefreshCw size={12} /> Retry</button>
            </div>
          ) : null}

          <div className="call-controls">
            {selectedIsVav ? (
              selectedReady && selectedPhoneNumber ? (
                <a className="call-button" href={`tel:${selectedPhoneNumber}`}><PhoneCall size={15} /> Call {selectedPhoneNumber}</a>
              ) : (
                <button className="call-button" disabled title={selected ? agentTestReadinessMessage(selected, selectedRuntimeProfile) : 'Select a voice agent before testing.'}><PhoneCall size={15} /> Phone test unavailable</button>
              )
            ) : !active ? (
              <button
                className="call-button"
                disabled={!browserTestReady || agentsLoading}
                title={browserTestReady ? undefined : selected ? agentTestReadinessMessage(selected, selectedRuntimeProfile) : 'Select a voice agent before testing.'}
                onClick={() => void startSession()}
              ><Play size={15} /> {state === 'error' || state === 'ended' ? 'Start new test' : 'Start test'}</button>
            ) : (
              <>
                <button className="icon-button" style={{ background: 'rgba(255,255,255,.09)', borderColor: 'rgba(255,255,255,.12)', color: 'white' }} onClick={toggleMute} aria-label={muted ? 'Unmute microphone' : 'Mute microphone'}>{muted ? <MicOff size={17} /> : <Mic size={17} />}</button>
                <button className="call-button end" onClick={endSession}><PhoneOff size={15} /> End call</button>
              </>
            )}
          </div>
        </section>
      </div>
    </Layout>
  );
}

function sessionLabel(state: SessionState) {
  const labels: Record<SessionState, string> = {
    idle: 'Ready to test',
    connecting: 'Connecting securely…',
    listening: 'Listening',
    speaking: 'Agent is speaking',
    ended: 'Session complete',
    error: 'Connection issue',
  };
  return labels[state];
}

function sessionDescription(state: SessionState, selected: VoiceAgent | undefined, selectedReady: boolean, runtimeProfile?: RuntimeProfile) {
  if (state === 'idle') {
    if (!selected) return 'Select a voice agent to begin';
    return selectedReady ? 'Start a private test conversation' : agentTestReadinessMessage(selected, runtimeProfile);
  }
  if (state === 'speaking') return 'Agent audio is streaming';
  if (state === 'listening') return 'Speak naturally — interruption is supported';
  if (state === 'connecting') return 'Preparing microphone, token, and secure connection';
  return 'Review the transcript and diagnostics before another test';
}

