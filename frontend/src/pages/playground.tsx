import { useRouter } from 'next/router';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Bot,
  FlaskConical,
  MessageSquareText,
  Mic,
  MicOff,
  PhoneOff,
  Play,
  ShieldCheck,
  Volume2,
} from 'lucide-react';
import Layout from '@/components/Layout';
import { api, VoiceAgent } from '@/lib/api';
import type { AtomsAgent } from '@smallest-ai/agent-sdk';

type SessionState = 'idle' | 'connecting' | 'listening' | 'speaking' | 'ended' | 'error';
type TranscriptTurn = { role: 'assistant' | 'user'; text: string };

export default function Playground() {
  const router = useRouter();
  const [agents, setAgents] = useState<VoiceAgent[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [state, setState] = useState<SessionState>('idle');
  const [transcript, setTranscript] = useState<TranscriptTurn[]>([]);
  const [muted, setMuted] = useState(false);
  const [variablesText, setVariablesText] = useState('{\n  "customer_name": "Vimal"\n}');
  const [error, setError] = useState('');
  const agentRef = useRef<AtomsAgent | null>(null);

  useEffect(() => {
    api.listAgents().then((items) => {
      setAgents(items);
      const requested = typeof router.query.agent === 'string' ? router.query.agent : '';
      setSelectedId(requested && items.some((agent) => agent.id === requested) ? requested : items[0]?.id || '');
    }).catch(() => setAgents([]));
  }, [router.query.agent]);

  useEffect(() => () => agentRef.current?.disconnect(), []);

  const selected = useMemo(() => agents.find((agent) => agent.id === selectedId), [agents, selectedId]);
  const active = ['connecting', 'listening', 'speaking'].includes(state);

  const startSession = async () => {
    if (!selected?.provider_agent_id) return;
    setState('connecting');
    setError('');
    setTranscript([]);
    try {
      const variables = JSON.parse(variablesText) as Record<string, string | number | boolean>;
      const session = await api.createSmallestSession(selected.id, variables);
      const { AtomsAgent } = await import('@smallest-ai/agent-sdk');
      const voiceAgent = new AtomsAgent({
        apiKey: session.access_token,
        agentId: selected.provider_agent_id,
        sampleRate: session.sample_rate,
      });
      agentRef.current = voiceAgent;
      voiceAgent.on('session_started', () => setState('listening'));
      voiceAgent.on('agent_start_talking', () => setState('speaking'));
      voiceAgent.on('agent_stop_talking', () => setState('listening'));
      voiceAgent.on('transcript', (event: { role: 'assistant' | 'user'; text: string }) => {
        setTranscript((current) => [...current, { role: event.role, text: event.text }]);
      });
      voiceAgent.on('session_ended', () => setState('ended'));
      voiceAgent.on('error', (event: { message?: string }) => {
        setError(event.message || 'The voice session ended unexpectedly.');
        setState('error');
      });
      await voiceAgent.connect();
    } catch (sessionError) {
      setError(sessionError instanceof Error ? sessionError.message : 'Could not start the voice session.');
      setState('error');
    }
  };

  const endSession = () => {
    agentRef.current?.disconnect();
    agentRef.current = null;
    setState('ended');
    setMuted(false);
  };

  const toggleMute = () => {
    if (!agentRef.current) return;
    if (muted) agentRef.current.unmute(); else agentRef.current.mute();
    setMuted((value) => !value);
  };

  return (
    <Layout>
      <div className="page-header">
        <div><span className="page-kicker">Safe test environment</span><h1>Voice playground</h1><p className="page-subtitle">Talk to the live Atoms agent in your browser using a 30-second, single-use token. Your Smallest.ai API key never reaches this page.</p></div>
        <span className="badge badge-success"><ShieldCheck size={12} /> Secure token flow</span>
      </div>

      <div className="playground-layout">
        <section className="card playground-config">
          <div className="card-title"><div><h3>Test configuration</h3><p>Choose an agent and optional call context.</p></div><FlaskConical size={17} color="var(--accent)" /></div>
          <div className="form-group"><label>Voice agent</label><select value={selectedId} disabled={active} onChange={(event) => setSelectedId(event.target.value)}><option value="">Select an agent</option>{agents.map((agent) => <option value={agent.id} key={agent.id}>{agent.name}{agent.provider_agent_id ? '' : ' — local only'}</option>)}</select></div>
          {selected && <div className="provider-alert"><Bot size={15} /><div><strong>{selected.name}</strong><br />{selected.provider_agent_id ? `Smallest.ai · ${selected.language.toUpperCase()} · ${selected.model_name}` : 'Provision this local draft before testing.'}</div></div>}
          <div className="form-group"><label>Pre-call variables <span>JSON, scalar values only</span></label><textarea value={variablesText} disabled={active} onChange={(event) => setVariablesText(event.target.value)} /><p className="form-hint">These values fill matching prompt variables only for this test call.</p></div>
          <div className="card" style={{ background: 'var(--bg-muted)', boxShadow: 'none' }}><div className="activity-list"><div className="activity-item"><div className="activity-icon"><Mic size={14} /></div><div><strong>Microphone permission</strong><p>Requested only after Start test</p></div></div><div className="activity-item"><div className="activity-icon"><Volume2 size={14} /></div><div><strong>Live audio output</strong><p>24 kHz streaming playback</p></div></div><div className="activity-item"><div className="activity-icon"><MessageSquareText size={14} /></div><div><strong>Transcript events</strong><p>Settled turn-by-turn in this session</p></div></div></div></div>
        </section>

        <section className="card voice-stage">
          <div className="voice-stage-header"><div><h3>{selected?.name || 'Select an agent'}</h3><p>Smallest.ai Atoms browser session</p></div><span className={`badge ${active ? 'badge-success' : 'badge-neutral'}`}>{state}</span></div>
          <div className="voice-orb-wrap"><div><div className={`voice-orb ${state === 'listening' || state === 'speaking' ? 'listening' : ''}`} /><div className="session-status" style={{ marginTop: 28 }}><strong>{sessionLabel(state)}</strong><span>{state === 'idle' ? 'Start a private test conversation' : state === 'speaking' ? 'Agent audio is streaming' : state === 'listening' ? 'Speak naturally — interruption is supported' : 'Review the session transcript below'}</span></div></div></div>
          <div className="transcript-panel">
            {transcript.length === 0 ? <div className="transcript-empty"><div><MessageSquareText size={20} style={{ marginBottom: 8 }} /><br />Live transcript will appear here after the first turn.</div></div> : transcript.map((turn, index) => <div className={`transcript-turn ${turn.role}`} key={`${turn.role}-${index}`}><small>{turn.role}</small>{turn.text}</div>)}
          </div>
          {error && <div className="provider-alert badge-danger" style={{ marginTop: 12 }}>{error}</div>}
          <div className="call-controls">
            {!active ? <button className="call-button" disabled={!selected?.provider_agent_id} onClick={startSession}><Play size={15} /> Start test</button> : <><button className="icon-button" style={{ background: 'rgba(255,255,255,.09)', borderColor: 'rgba(255,255,255,.12)', color: 'white' }} onClick={toggleMute} aria-label={muted ? 'Unmute' : 'Mute'}>{muted ? <MicOff size={17} /> : <Mic size={17} />}</button><button className="call-button end" onClick={endSession}><PhoneOff size={15} /> End call</button></>}
          </div>
        </section>
      </div>
    </Layout>
  );
}

function sessionLabel(state: SessionState) {
  const labels: Record<SessionState, string> = { idle: 'Ready to test', connecting: 'Connecting securely…', listening: 'Listening', speaking: 'Agent is speaking', ended: 'Session complete', error: 'Connection issue' };
  return labels[state];
}
