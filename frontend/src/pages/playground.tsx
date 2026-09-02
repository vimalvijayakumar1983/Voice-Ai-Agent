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
import type { RemoteAudioTrack, Room } from 'livekit-client';

type SessionState = 'idle' | 'connecting' | 'initializing' | 'listening' | 'thinking' | 'speaking' | 'reconnecting' | 'ended' | 'error';
type BrowserTransport = 'smallest' | 'livekit' | null;
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
  roomName: string;
  participantIdentity: string;
  callId: string;
  agentState: string;
  audioPlayback: 'not started' | 'pending' | 'ready' | 'playing' | 'blocked';
  maxDurationSeconds: number | null;
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
  roomName: '',
  participantIdentity: '',
  callId: '',
  agentState: 'not connected',
  audioPlayback: 'not started',
  maxDurationSeconds: null,
  eventCount: 0,
  lastEvent: 'No session events',
  endReason: '',
};

const CONNECT_TIMEOUT_MS = 12_000;
const AGENT_JOIN_TIMEOUT_MS = 20_000;

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

function liveKitSessionState(agentState: string): SessionState | null {
  if (agentState === 'speaking') return 'speaking';
  if (agentState === 'thinking') return 'thinking';
  if (agentState === 'listening' || agentState === 'idle') return 'listening';
  if (agentState === 'initializing' || agentState === 'pre-connect-buffering') return 'initializing';
  if (agentState === 'failed') return 'error';
  if (agentState === 'disconnected') return 'ended';
  return null;
}

function timeoutAfter(milliseconds: number, message: string) {
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  const promise = new Promise<never>((_, reject) => {
    timeoutId = setTimeout(() => reject(new Error(message)), milliseconds);
  });
  return { promise, clear: () => timeoutId && clearTimeout(timeoutId) };
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
  const smallestAgentRef = useRef<AtomsAgent | null>(null);
  const liveKitRoomRef = useRef<Room | null>(null);
  const liveKitAudioTracksRef = useRef<Set<RemoteAudioTrack>>(new Set());
  const liveKitFinalSegmentsRef = useRef<Set<string>>(new Set());
  const liveKitAgentJoinTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const muteOperationRef = useRef(false);
  const attemptRef = useRef(false);
  const terminalStateRef = useRef<'ended' | 'error' | null>(null);
  const turnIdRef = useRef(0);
  const connectStartedAtRef = useRef(0);
  const transcriptPanelRef = useRef<HTMLDivElement>(null);
  const remoteAudioRef = useRef<HTMLAudioElement>(null);
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

  useEffect(() => () => {
    terminalStateRef.current = 'ended';
    if (liveKitAgentJoinTimerRef.current) clearTimeout(liveKitAgentJoinTimerRef.current);
    liveKitAgentJoinTimerRef.current = null;
    smallestAgentRef.current?.disconnect();
    smallestAgentRef.current = null;
    liveKitAudioTracksRef.current.forEach((track) => track.detach());
    liveKitAudioTracksRef.current.clear();
    const room = liveKitRoomRef.current;
    liveKitRoomRef.current = null;
    if (room) {
      void room.localParticipant.setMicrophoneEnabled(false).catch(() => undefined);
      void room.disconnect(true);
    }
  }, []);

  const selected = useMemo(
    () => agents.find((agent) => agent.id === selectedId),
    [agents, selectedId],
  );
  const selectedRuntimeProfile = selected ? runtimeProfiles[selected.id] : undefined;
  const selectedReady = isAgentCallReady(selected, selectedRuntimeProfile);
  const selectedIsVav = ['sarvam', 'elevenlabs', 'inworld'].includes(selected?.voice_provider ?? '');
  const selectedUsesLiveKitBrowser = Boolean(
    selected?.is_active
    && selected.voice_provider === 'inworld'
    && selectedRuntimeProfile?.id
    && selectedRuntimeProfile.status !== 'inactive'
    && selectedRuntimeProfile.telephony_provider === 'livekit_sip'
    && selectedRuntimeProfile.primary_speech_provider === 'inworld'
    && selectedRuntimeProfile.llm_provider === 'inworld',
  );
  const browserTransport: BrowserTransport = selectedUsesLiveKitBrowser
    ? 'livekit'
    : selected && !selectedIsVav
      ? 'smallest'
      : null;
  const selectedPhoneNumber = selectedRuntimeProfile?.assigned_numbers[0] || '';
  // LiveKit browser testing is intentionally independent from DID/SIP phone
  // readiness. The backend remains authoritative for worker, credential, KB,
  // capacity, and budget blockers when it issues the room-scoped token.
  const browserTestAvailable = browserTransport === 'livekit'
    ? selectedUsesLiveKitBrowser
    : browserTransport === 'smallest' && selectedReady;
  const phoneTestReady = selectedReady && selectedIsVav && Boolean(selectedPhoneNumber);
  const phoneOnlySelected = selectedIsVav && !selectedUsesLiveKitBrowser;
  const active = ['connecting', 'initializing', 'listening', 'thinking', 'speaking', 'reconnecting'].includes(state);
  const selectedLanguages = useMemo(() => {
    if (!selected) return [];
    return Array.from(new Set([selected.language, ...(selected.supported_languages || [])].filter(Boolean)));
  }, [selected]);
  const multilingualConfigured = selectedLanguages.length > 1;

  useEffect(() => {
    // Router-driven agent changes can occur even while the selector is locked.
    // Tear down the previous agent before resetting its visible state so no
    // microphone or provider session can continue behind the new selection.
    terminalStateRef.current = 'ended';
    smallestAgentRef.current?.disconnect();
    smallestAgentRef.current = null;
    if (liveKitAgentJoinTimerRef.current) clearTimeout(liveKitAgentJoinTimerRef.current);
    liveKitAgentJoinTimerRef.current = null;
    const previousRoom = liveKitRoomRef.current;
    liveKitRoomRef.current = null;
    if (previousRoom) {
      void previousRoom.localParticipant.setMicrophoneEnabled(false).catch(() => undefined);
      void previousRoom.disconnect(true);
    }
    liveKitAudioTracksRef.current.forEach((track) => track.detach());
    liveKitAudioTracksRef.current.clear();
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
    if (!selected || !browserTransport || !browserTestAvailable) {
      setError({
        title: 'Agent is not ready to test',
        message: selectedIsVav && !selectedUsesLiveKitBrowser
          ? 'This VAV realtime configuration supports phone testing only. LiveKit browser testing requires an active Inworld + LiveKit runtime.'
          : selectedUsesLiveKitBrowser
            ? 'The browser lane is available independently from the phone route. Retry to receive the exact LiveKit, Inworld, worker, knowledge, capacity, or budget blocker.'
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
    smallestAgentRef.current?.disconnect();
    smallestAgentRef.current = null;
    if (liveKitAgentJoinTimerRef.current) clearTimeout(liveKitAgentJoinTimerRef.current);
    liveKitAgentJoinTimerRef.current = null;
    liveKitAudioTracksRef.current.forEach((track) => track.detach());
    liveKitAudioTracksRef.current.clear();
    if (liveKitRoomRef.current) void liveKitRoomRef.current.disconnect(true);
    liveKitRoomRef.current = null;
    liveKitFinalSegmentsRef.current.clear();
    setState('connecting');
    setError(null);
    dispatchTranscript({ type: 'clear' });
    setMuted(false);
    setDiagnostics({ ...INITIAL_DIAGNOSTICS, lastEvent: 'Preparing microphone' });

    try {
      // Permission and the lazy SDK download happen before token issuance so a
      // short-lived credential is never spent waiting on either.
      await requestMicrophoneReadiness();
      if (terminalStateRef.current === 'ended' || terminalStateRef.current === 'error') return;
      setDiagnostics((current) => ({ ...current, permission: 'granted', lastEvent: 'Microphone ready' }));

      if (browserTransport === 'smallest') {
        const { AtomsAgent } = await import('@smallest-ai/agent-sdk');
        if (terminalStateRef.current === 'ended') return;
        setDiagnostics((current) => ({ ...current, token: 'requesting', lastEvent: 'Requesting secure token' }));
        const session = await api.createSmallestSession(selected.id, variables);
        if (terminalStateRef.current === 'ended') return;
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
        smallestAgentRef.current = voiceAgent;
        const isCurrentSmallestAgent = () => smallestAgentRef.current === voiceAgent;
        connectStartedAtRef.current = performance.now();

        voiceAgent.on('session_started', (event) => {
          if (!isCurrentSmallestAgent() || terminalStateRef.current !== null) {
            if (isCurrentSmallestAgent()) smallestAgentRef.current = null;
            voiceAgent.disconnect();
            return;
          }
          setState('listening');
          setDiagnostics((current) => ({
            ...current,
            token: 'consumed',
            connectTimeMs: Math.round(performance.now() - connectStartedAtRef.current),
            sessionId: event.session_id,
            callId: event.call_id,
            agentState: 'listening',
            audioPlayback: 'playing',
            eventCount: current.eventCount + 1,
            lastEvent: 'Session started',
          }));
          void api.registerBrowserConversation(selected.id, event.call_id).catch((registrationError) => {
            if (!isCurrentSmallestAgent()) return;
            setDiagnostics((current) => ({
              ...current,
              lastEvent: registrationError instanceof Error
                ? `Session started; history registration failed: ${registrationError.message}`
                : 'Session started; history registration failed',
            }));
          });
        });
        voiceAgent.on('agent_start_talking', () => {
          if (!isCurrentSmallestAgent()) return;
          setState('speaking');
          setDiagnostics((current) => ({ ...current, agentState: 'speaking' }));
          recordEvent('Agent started speaking');
        });
        voiceAgent.on('agent_stop_talking', () => {
          if (!isCurrentSmallestAgent()) return;
          setState('listening');
          setDiagnostics((current) => ({ ...current, agentState: 'listening' }));
          recordEvent('Agent stopped speaking');
        });
        voiceAgent.on('transcript_delta', (event) => {
          if (!isCurrentSmallestAgent()) return;
          if (!event.text.trim()) return;
          dispatchTranscript({ type: 'delta', role: event.role, text: event.text });
          recordEvent(`${event.role === 'assistant' ? 'Agent' : 'User'} transcript update`);
        });
        voiceAgent.on('transcript', (event) => {
          if (!isCurrentSmallestAgent()) return;
          const text = event.text.trim();
          if (!text) return;
          turnIdRef.current += 1;
          dispatchTranscript({ type: 'settled', id: turnIdRef.current, role: event.role, text });
          recordEvent(`${event.role === 'assistant' ? 'Agent' : 'User'} turn settled`);
        });
        voiceAgent.on('session_ended', (event) => {
          if (!isCurrentSmallestAgent()) return;
          smallestAgentRef.current = null;
          setMuted(false);
          dispatchTranscript({ type: 'clear_live' });
          setDiagnostics((current) => ({
            ...current,
            agentState: terminalStateRef.current === 'error' ? 'failed' : 'disconnected',
            audioPlayback: 'not started',
            eventCount: current.eventCount + 1,
            lastEvent: 'Session ended',
            endReason: terminalStateRef.current === 'error'
              ? current.endReason
              : event.reason || 'Provider ended the session',
          }));
          if (terminalStateRef.current !== 'error') setState('ended');
        });
        voiceAgent.on('error', (event) => {
          if (!isCurrentSmallestAgent()) return;
          terminalStateRef.current = 'error';
          smallestAgentRef.current = null;
          setMuted(false);
          setError(sessionErrorGuidance(new Error(event.message || event.code)));
          setState('error');
          setDiagnostics((current) => ({
            ...current,
            token: current.token === 'requesting' ? 'failed' : current.token,
            agentState: 'failed',
            eventCount: current.eventCount + 1,
            lastEvent: event.code ? `Provider error: ${event.code}` : 'Provider error',
            endReason: event.message || event.code || 'Provider error',
          }));
          voiceAgent.disconnect();
        });

        const timeout = timeoutAfter(CONNECT_TIMEOUT_MS, 'WebSocket connection timed out.');
        try {
          await Promise.race([voiceAgent.connect(), timeout.promise]);
        } finally {
          timeout.clear();
        }
      } else {
        // livekit-client is intentionally absent from the initial/server bundle.
        // It is loaded only after a deliberate, permission-granting user action.
        const {
          DisconnectReason,
          Room: LiveKitRoom,
          RoomEvent,
          Track,
        } = await import('livekit-client');
        if (terminalStateRef.current === 'ended') return;
        setDiagnostics((current) => ({ ...current, token: 'requesting', lastEvent: 'Requesting secure LiveKit token' }));
        const session = await api.createLiveKitBrowserSession(selected.id, variables);
        if (terminalStateRef.current === 'ended') return;
        setDiagnostics((current) => ({
          ...current,
          token: 'issued',
          tokenLifetimeSeconds: session.expires_in,
          roomName: session.room_name,
          participantIdentity: session.participant_identity,
          callId: session.call_id,
          maxDurationSeconds: session.max_duration_seconds,
          audioPlayback: 'pending',
          lastEvent: 'Room-scoped token issued',
        }));

        const room = new LiveKitRoom({ adaptiveStream: true, dynacast: true });
        liveKitRoomRef.current = room;
        connectStartedAtRef.current = performance.now();

        const isCurrentRoom = () => liveKitRoomRef.current === room;
        const clearAgentJoinTimeout = () => {
          if (liveKitAgentJoinTimerRef.current) clearTimeout(liveKitAgentJoinTimerRef.current);
          liveKitAgentJoinTimerRef.current = null;
        };
        const teardownLiveKitRoom = () => {
          if (!isCurrentRoom()) return;
          clearAgentJoinTimeout();
          liveKitRoomRef.current = null;
          room.unregisterTextStreamHandler('lk.transcription');
          liveKitAudioTracksRef.current.forEach((track) => track.detach());
          liveKitAudioTracksRef.current.clear();
          setMuted(false);
          dispatchTranscript({ type: 'clear_live' });
          void room.localParticipant.setMicrophoneEnabled(false).catch(() => undefined);
          void room.disconnect(true);
        };

        const applyAgentState = (participant: { isAgent: boolean; attributes: Record<string, string> }) => {
          if (!participant.isAgent || !isCurrentRoom()) return;
          clearAgentJoinTimeout();
          const agentState = participant.attributes['lk.agent.state'] || 'initializing';
          const mappedState = liveKitSessionState(agentState);
          setDiagnostics((current) => ({ ...current, agentState, lastEvent: `Agent state: ${agentState}` }));
          if (mappedState) setState(mappedState);
          if (mappedState === 'error') {
            terminalStateRef.current = 'error';
            setError({
              title: 'LiveKit agent reported a failure',
              message: 'The browser reached the room, but the Inworld agent could not continue. Review worker readiness and the call diagnostics.',
            });
            setDiagnostics((current) => ({
              ...current,
              audioPlayback: 'not started',
              endReason: 'Inworld agent reported a failure',
            }));
            teardownLiveKitRoom();
          } else if (mappedState === 'ended') {
            terminalStateRef.current = 'ended';
            setDiagnostics((current) => ({
              ...current,
              audioPlayback: 'not started',
              endReason: 'Inworld agent ended the session',
            }));
            teardownLiveKitRoom();
          }
        };

        const armAgentJoinTimeout = () => {
          clearAgentJoinTimeout();
          liveKitAgentJoinTimerRef.current = setTimeout(() => {
            if (!isCurrentRoom()) return;
            liveKitAgentJoinTimerRef.current = null;
            const agentJoined = Array.from(room.remoteParticipants.values())
              .some((participant) => participant.isAgent);
            if (agentJoined) return;
            terminalStateRef.current = 'error';
            setError({
              title: 'LiveKit agent did not join',
              message: 'The room connected, but the VAV Inworld worker did not join within 20 seconds. Check worker registration and retry.',
            });
            setState('error');
            setDiagnostics((current) => ({
              ...current,
              agentState: 'failed',
              audioPlayback: 'not started',
              lastEvent: 'Agent join timed out',
              endReason: 'VAV Inworld worker did not join within 20 seconds',
            }));
            teardownLiveKitRoom();
          }, AGENT_JOIN_TIMEOUT_MS);
        };

        room.on(RoomEvent.Connected, () => {
          if (!isCurrentRoom()) return;
          setState('initializing');
          setDiagnostics((current) => ({
            ...current,
            token: 'consumed',
            connectTimeMs: Math.round(performance.now() - connectStartedAtRef.current),
            eventCount: current.eventCount + 1,
            lastEvent: 'LiveKit room connected',
          }));
          const agent = Array.from(room.remoteParticipants.values())
            .find((participant) => participant.isAgent);
          if (agent) applyAgentState(agent); else armAgentJoinTimeout();
        });
        room.on(RoomEvent.ParticipantConnected, (participant) => {
          if (!isCurrentRoom()) return;
          recordEvent(participant.isAgent ? 'Inworld agent joined' : 'Remote participant joined');
          applyAgentState(participant);
        });
        room.on(RoomEvent.ParticipantDisconnected, (participant) => {
          if (!isCurrentRoom() || !participant.isAgent) return;
          terminalStateRef.current = 'error';
          setError({
            title: 'LiveKit agent disconnected',
            message: 'The VAV Inworld worker left the room unexpectedly. Review worker health and this call record, then retry.',
          });
          setState('error');
          setDiagnostics((current) => ({
            ...current,
            agentState: 'failed',
            audioPlayback: 'not started',
            lastEvent: 'Inworld agent disconnected',
            endReason: 'VAV Inworld worker left the room unexpectedly',
          }));
          teardownLiveKitRoom();
        });
        room.on(RoomEvent.ParticipantAttributesChanged, (_changedAttributes, participant) => {
          if (!isCurrentRoom()) return;
          applyAgentState(participant);
        });
        room.on(RoomEvent.Reconnecting, () => {
          if (!isCurrentRoom()) return;
          setState('reconnecting');
          recordEvent('LiveKit reconnecting');
        });
        room.on(RoomEvent.Reconnected, () => {
          if (!isCurrentRoom()) return;
          const agent = Array.from(room.remoteParticipants.values()).find((participant) => participant.isAgent);
          if (agent) applyAgentState(agent); else {
            setState('initializing');
            armAgentJoinTimeout();
          }
          recordEvent('LiveKit reconnected');
        });
        room.on(RoomEvent.AudioPlaybackStatusChanged, (playing) => {
          if (!isCurrentRoom()) return;
          setDiagnostics((current) => ({
            ...current,
            audioPlayback: playing ? 'ready' : 'blocked',
            lastEvent: playing ? 'Audio playback ready' : 'Audio playback needs a click',
          }));
        });
        room.on(RoomEvent.TrackSubscribed, (track) => {
          if (!isCurrentRoom() || track.kind !== Track.Kind.Audio || !remoteAudioRef.current) return;
          const audioTrack = track as RemoteAudioTrack;
          const audioElement = remoteAudioRef.current;
          liveKitAudioTracksRef.current.add(audioTrack);
          audioTrack.attach(audioElement);
          void audioElement.play().then(() => {
            if (!isCurrentRoom() || remoteAudioRef.current !== audioElement) return;
            setDiagnostics((current) => ({ ...current, audioPlayback: 'playing', lastEvent: 'Agent audio subscribed' }));
          }).catch(() => {
            if (!isCurrentRoom() || remoteAudioRef.current !== audioElement) return;
            setDiagnostics((current) => ({ ...current, audioPlayback: 'blocked', lastEvent: 'Click Enable sound to hear the agent' }));
          });
          recordEvent('Agent audio track subscribed');
        });
        room.on(RoomEvent.TrackUnsubscribed, (track) => {
          if (!isCurrentRoom() || track.kind !== Track.Kind.Audio) return;
          const audioTrack = track as RemoteAudioTrack;
          audioTrack.detach();
          liveKitAudioTracksRef.current.delete(audioTrack);
          recordEvent('Agent audio track ended');
        });
        room.registerTextStreamHandler('lk.transcription', (reader, participantInfo) => {
          void reader.readAll().then((rawText) => {
            if (!isCurrentRoom()) return;
            const text = rawText.trim();
            if (!text) return;
            const attributes = reader.info.attributes || {};
            const segmentId = attributes['lk.segment_id'] || reader.info.id;
            const final = attributes['lk.transcription_final'] === 'true';
            const role = participantInfo.identity === room.localParticipant.identity
              ? 'user'
              : 'assistant';
            if (final) {
              if (liveKitFinalSegmentsRef.current.has(segmentId)) return;
              liveKitFinalSegmentsRef.current.add(segmentId);
              turnIdRef.current += 1;
              dispatchTranscript({ type: 'settled', id: turnIdRef.current, role, text });
              recordEvent(`${role === 'assistant' ? 'Agent' : 'User'} turn settled`);
            } else {
              dispatchTranscript({ type: 'delta', role, text });
            }
          }).catch((transcriptionError) => {
            if (!isCurrentRoom()) return;
            setDiagnostics((current) => ({
              ...current,
              lastEvent: transcriptionError instanceof Error
                ? `Transcription stream failed: ${transcriptionError.message}`
                : 'Transcription stream failed',
            }));
          });
        });
        room.on(RoomEvent.DataReceived, (_payload, _participant, _kind, topic) => {
          if (!isCurrentRoom()) return;
          recordEvent(topic ? `Data event: ${topic}` : 'LiveKit data event');
        });
        room.on(RoomEvent.LocalAudioSilenceDetected, () => {
          if (!isCurrentRoom()) return;
          setDiagnostics((current) => ({ ...current, lastEvent: 'Microphone is connected but silent' }));
        });
        room.on(RoomEvent.MediaDevicesError, (mediaError) => {
          if (!isCurrentRoom()) return;
          terminalStateRef.current = 'error';
          setError(sessionErrorGuidance(mediaError));
          setState('error');
          setDiagnostics((current) => ({
            ...current,
            agentState: 'failed',
            audioPlayback: 'not started',
            lastEvent: 'Microphone device error',
            endReason: mediaError.message,
          }));
          teardownLiveKitRoom();
        });
        room.on(RoomEvent.Disconnected, (reason) => {
          // A superseded/explicitly cleaned-up room must not overwrite the
          // diagnostics of the current session or its original failure.
          if (liveKitRoomRef.current !== room) return;
          clearAgentJoinTimeout();
          liveKitRoomRef.current = null;
          room.unregisterTextStreamHandler('lk.transcription');
          liveKitAudioTracksRef.current.forEach((track) => track.detach());
          liveKitAudioTracksRef.current.clear();
          setMuted(false);
          dispatchTranscript({ type: 'clear_live' });
          const reasonLabel = reason === undefined
            ? 'Room disconnected'
            : DisconnectReason[reason] || `Disconnect reason ${reason}`;
          const reachedMaximumDuration = (
            performance.now() - connectStartedAtRef.current
          ) >= Math.max((session.max_duration_seconds * 1000) - 5_000, 0);
          if (reachedMaximumDuration) terminalStateRef.current = 'ended';
          else {
            terminalStateRef.current = 'error';
            setError({
              title: 'LiveKit session disconnected',
              message: `The browser session ended unexpectedly (${reasonLabel}). Check the network and worker health before retrying.`,
            });
          }
          setDiagnostics((current) => ({
            ...current,
            agentState: terminalStateRef.current === 'error' ? 'failed' : 'disconnected',
            audioPlayback: 'not started',
            eventCount: current.eventCount + 1,
            lastEvent: 'LiveKit room disconnected',
            endReason: reachedMaximumDuration ? 'Configured maximum call duration reached' : reasonLabel,
          }));
          setState(terminalStateRef.current === 'error' ? 'error' : 'ended');
        });

        const timeout = timeoutAfter(CONNECT_TIMEOUT_MS, 'LiveKit WebSocket connection timed out.');
        try {
          await Promise.race([
            room.connect(session.url, session.access_token, { autoSubscribe: true }),
            timeout.promise,
          ]);
        } finally {
          timeout.clear();
        }
        if (!isCurrentRoom()) return;
        await room.startAudio().then(() => {
          if (isCurrentRoom()) setDiagnostics((current) => ({ ...current, audioPlayback: 'ready' }));
        }).catch(() => {
          if (isCurrentRoom()) setDiagnostics((current) => ({ ...current, audioPlayback: 'blocked' }));
        });
        if (!isCurrentRoom()) return;
        await room.localParticipant.setMicrophoneEnabled(true);
        if (!isCurrentRoom()) {
          await room.localParticipant.setMicrophoneEnabled(false).catch(() => undefined);
          return;
        }
        setDiagnostics((current) => ({ ...current, lastEvent: 'Microphone published to LiveKit' }));
        room.remoteParticipants.forEach((participant) => applyAgentState(participant));
      }
    } catch (sessionError) {
      // Ending a still-connecting session is intentional, not a connection
      // failure. Explicit or event-driven terminal handlers already performed
      // the full teardown and must retain their original diagnostics.
      if (terminalStateRef.current === 'ended' || terminalStateRef.current === 'error') return;
      terminalStateRef.current = 'error';
      smallestAgentRef.current?.disconnect();
      smallestAgentRef.current = null;
      if (liveKitAgentJoinTimerRef.current) clearTimeout(liveKitAgentJoinTimerRef.current);
      liveKitAgentJoinTimerRef.current = null;
      liveKitAudioTracksRef.current.forEach((track) => track.detach());
      liveKitAudioTracksRef.current.clear();
      const room = liveKitRoomRef.current;
      liveKitRoomRef.current = null;
      if (room) {
        await room.localParticipant.setMicrophoneEnabled(false).catch(() => undefined);
        await room.disconnect(true).catch(() => undefined);
      }
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
        agentState: 'failed',
        audioPlayback: 'not started',
        lastEvent: 'Connection failed',
        endReason: sessionError instanceof Error ? sessionError.message : 'Unknown connection error',
      }));
    } finally {
      attemptRef.current = false;
    }
  };

  const endSession = () => {
    terminalStateRef.current = 'ended';
    smallestAgentRef.current?.disconnect();
    smallestAgentRef.current = null;
    if (liveKitAgentJoinTimerRef.current) clearTimeout(liveKitAgentJoinTimerRef.current);
    liveKitAgentJoinTimerRef.current = null;
    const room = liveKitRoomRef.current;
    liveKitRoomRef.current = null;
    if (room) {
      void room.localParticipant.setMicrophoneEnabled(false).catch(() => undefined);
      void room.disconnect(true);
    }
    liveKitAudioTracksRef.current.forEach((track) => track.detach());
    liveKitAudioTracksRef.current.clear();
    dispatchTranscript({ type: 'clear_live' });
    setState('ended');
    setMuted(false);
    setDiagnostics((current) => ({
      ...current,
      agentState: 'disconnected',
      audioPlayback: 'not started',
      eventCount: current.eventCount + 1,
      lastEvent: 'Browser test ended by user',
      endReason: 'User ended the browser test',
    }));
  };

  const toggleMute = async () => {
    if (muteOperationRef.current) return;
    muteOperationRef.current = true;
    const targetMuted = !muted;
    try {
      if (smallestAgentRef.current) {
        if (targetMuted) smallestAgentRef.current.mute(); else smallestAgentRef.current.unmute();
      } else if (liveKitRoomRef.current) {
        const room = liveKitRoomRef.current;
        try {
          await room.localParticipant.setMicrophoneEnabled(!targetMuted);
          if (liveKitRoomRef.current !== room) {
            await room.localParticipant.setMicrophoneEnabled(false).catch(() => undefined);
            return;
          }
        } catch (muteError) {
          setError(sessionErrorGuidance(muteError));
          return;
        }
      } else {
        return;
      }
      setMuted(targetMuted);
      recordEvent(targetMuted ? 'Microphone muted' : 'Microphone unmuted');
    } finally {
      muteOperationRef.current = false;
    }
  };

  const enableLiveKitAudio = async () => {
    const room = liveKitRoomRef.current;
    const audioElement = remoteAudioRef.current;
    if (!room || !audioElement) return;
    try {
      await room.startAudio();
      if (liveKitRoomRef.current !== room || remoteAudioRef.current !== audioElement) return;
      if (audioElement.srcObject) {
        await audioElement.play();
      }
      if (liveKitRoomRef.current !== room || remoteAudioRef.current !== audioElement) return;
      setDiagnostics((current) => ({
        ...current,
        audioPlayback: audioElement.srcObject ? 'playing' : 'ready',
        lastEvent: 'Audio playback enabled',
      }));
    } catch (audioError) {
      if (liveKitRoomRef.current !== room || remoteAudioRef.current !== audioElement) return;
      setDiagnostics((current) => ({
        ...current,
        audioPlayback: 'blocked',
        lastEvent: audioError instanceof Error ? `Audio blocked: ${audioError.message}` : 'Audio playback is blocked',
      }));
    }
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
                const runtime = runtimeProfiles[agent.id];
                const ready = isAgentCallReady(agent, runtime);
                const liveKitBrowser = Boolean(
                  agent.is_active
                  && agent.voice_provider === 'inworld'
                  && runtime?.id
                  && runtime.status !== 'inactive'
                  && runtime.telephony_provider === 'livekit_sip'
                  && runtime.primary_speech_provider === 'inworld'
                  && runtime.llm_provider === 'inworld',
                );
                const isVav = ['sarvam', 'elevenlabs', 'inworld'].includes(agent.voice_provider);
                const status = liveKitBrowser && !ready
                  ? ' — browser candidate · phone not ready'
                  : !ready
                  ? ' — not ready'
                  : liveKitBrowser
                    ? ' — browser candidate · phone ready'
                    : isVav
                      ? ' — phone ready'
                      : ' — browser ready';
                return <option value={agent.id} key={agent.id}>{agent.name}{status}</option>;
              })}
            </select>
          </div>

          {selected ? (
            <div className="provider-alert">
              <Bot size={15} aria-hidden="true" />
              <div>
                <strong>{selected.name}</strong>
                <p>{selectedUsesLiveKitBrowser
                  ? `LiveKit + Inworld browser test candidate, independent from e& SIP; live checks run when you start${phoneTestReady ? ` · phone ready at ${selectedPhoneNumber}` : ` · phone not ready: ${agentTestReadinessMessage(selected, selectedRuntimeProfile)}`}`
                  : selectedReady
                  ? selectedIsVav
                    ? `${selected.voice_provider === 'inworld' ? 'Inworld phone' : selected.voice_provider === 'elevenlabs' ? 'ElevenLabs voice phone' : 'Sarvam AI phone'} runtime active${selectedPhoneNumber ? ` · ${selectedPhoneNumber}` : ''}`
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
              <div className="activity-item"><div className="activity-icon"><Volume2 size={14} /></div><div><strong>Live audio output</strong><p>Transport and playback state are shown in diagnostics</p></div></div>
              <div className="activity-item"><div className="activity-icon"><MessageSquareText size={14} /></div><div><strong>Settled transcript</strong><p>Cumulative deltas update one live turn instead of duplicating text</p></div></div>
            </div>
          </div>
        </section>

        <section className="card voice-stage" aria-labelledby="voice-stage-heading">
          <div className="voice-stage-header">
            <div>
              <h2 id="voice-stage-heading" className={styles.stageHeading}>{selected?.name || 'Select an agent'}</h2>
              <p>{browserTransport === 'livekit'
                ? 'LiveKit WebRTC · Inworld STT, Router, and TTS'
                : browserTransport === 'smallest'
                  ? 'Smallest.ai Atoms browser session'
                  : `${selected?.voice_provider === 'inworld' ? 'Inworld' : selected?.voice_provider === 'elevenlabs' ? 'ElevenLabs' : 'Sarvam AI'} phone session`}</p>
            </div>
            <span className={`badge ${state === 'error' ? 'badge-danger' : active || browserTransport === 'smallest' && selectedReady || !browserTransport && selectedReady ? 'badge-success' : 'badge-neutral'}`}>
              {browserTransport ? active || state === 'ended' || state === 'error' ? state : browserTestAvailable ? browserTransport === 'livekit' ? 'checks pending' : 'ready' : 'not ready' : selectedReady ? 'active' : 'not ready'}
            </span>
          </div>

          <div className={styles.stageLanguageRow} aria-label="Test language configuration">
            {selectedLanguages.map((language) => <span key={language}>{languageName(language)}</span>)}
            {selected ? <small>{multilingualConfigured ? 'Switching requires a live pass' : 'Single-language configuration'}</small> : null}
          </div>

          <div className="voice-orb-wrap">
            <div>
              <div className={`voice-orb ${state === 'listening' || state === 'speaking' ? 'listening' : ''}`} aria-hidden="true" />
              <div className="session-status" style={{ marginTop: 28 }} aria-live="polite" aria-atomic="true">
                <strong>{phoneOnlySelected && selectedReady ? 'Phone runtime ready' : sessionLabel(state)}</strong>
                <span>{phoneOnlySelected && selectedReady
                  ? `Call ${selectedPhoneNumber} from any phone to test ${selected?.name || 'this agent'}.`
                  : sessionDescription(state, selected, browserTransport ? browserTestAvailable : selectedReady, selectedRuntimeProfile, selectedUsesLiveKitBrowser)}</span>
              </div>
            </div>
          </div>

          {phoneOnlySelected ? (
            <>
              <div className={styles.diagnostics} aria-label="Phone runtime diagnostics">
                <div><PhoneCall size={13} /><span>Phone number</span><strong>{selectedPhoneNumber || 'Not assigned'}</strong></div>
                <div><ShieldCheck size={13} /><span>Runtime</span><strong>{selectedRuntimeProfile?.status || 'not loaded'}</strong></div>
                <div><CheckCircle2 size={13} /><span>Readiness</span><strong>{selectedReady ? 'all gates passed' : 'attention required'}</strong></div>
                <div><Gauge size={13} /><span>Call limit</span><strong>{selectedRuntimeProfile?.daily_call_limit ?? '—'} / day</strong></div>
              </div>
              <div className={styles.diagnosticLine} aria-live="polite">
                <span>{selectedRuntimeProfile?.telephony_provider === 'livekit_sip' ? 'LiveKit SIP' : 'Twilio Media Streams'}</span>
                <span>{selected?.voice_provider === 'inworld' ? 'Direct Inworld STT · Router · TTS' : selected?.voice_provider === 'elevenlabs' ? 'ElevenLabs speech · Sarvam transcription' : 'Sarvam speech'}</span>
                <span>{selectedRuntimeProfile?.llm_provider === 'inworld' ? 'Inworld Router response engine' : 'OpenAI response engine'}</span>
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
                <div title={browserTransport === 'livekit' ? diagnostics.agentState : diagnostics.sessionId || undefined}>
                  <CheckCircle2 size={13} />
                  <span>{browserTransport === 'livekit' ? 'Agent state' : 'Session'}</span>
                  <strong>{browserTransport === 'livekit' ? diagnostics.agentState : shortIdentifier(diagnostics.sessionId)}</strong>
                </div>
              </div>

              <div className={styles.diagnosticLine} aria-live="polite">
                <span>{browserTransport === 'livekit'
                  ? 'LiveKit WebRTC audio'
                  : diagnostics.sampleRate
                    ? `${(diagnostics.sampleRate / 1000).toFixed(0)} kHz`
                    : 'Sample rate pending'}</span>
                {diagnostics.tokenLifetimeSeconds ? (
                  <span>{diagnostics.tokenLifetimeSeconds}s {browserTransport === 'livekit' ? 'room-scoped join token' : 'single-use token'}</span>
                ) : null}
                {diagnostics.roomName ? <span title={diagnostics.roomName}>Room {shortIdentifier(diagnostics.roomName)}</span> : null}
                {diagnostics.participantIdentity ? <span title={diagnostics.participantIdentity}>Participant {shortIdentifier(diagnostics.participantIdentity)}</span> : null}
                {browserTransport === 'livekit' ? <span>Audio {diagnostics.audioPlayback}</span> : null}
                {diagnostics.maxDurationSeconds ? <span>Maximum {Math.ceil(diagnostics.maxDurationSeconds / 60)} min</span> : null}
                <span>{diagnostics.eventCount} events</span>
                <span>{diagnostics.lastEvent}</span>
                {diagnostics.callId ? <span title={diagnostics.callId}>Call {shortIdentifier(diagnostics.callId)}</span> : null}
                {diagnostics.endReason ? <span>Ended: {diagnostics.endReason}</span> : null}
              </div>

              {browserTransport === 'livekit' ? (
                <>
                  <audio ref={remoteAudioRef} autoPlay aria-hidden="true" className={styles.remoteAudio} />
                  <div className={styles.carrierBoundaryNote}>
                    <AlertTriangle size={14} aria-hidden="true" />
                    <p><strong>Browser test does not use the e&amp; carrier line.</strong> Use the conversation to exercise LiveKit, Inworld, VAV knowledge, tools, turn-taking, and audio. Use <em>Call assigned number</em> for the complete e&amp; SIP test.</p>
                    {diagnostics.audioPlayback === 'blocked' ? (
                      <button type="button" onClick={() => void enableLiveKitAudio()}><Volume2 size={12} /> Enable sound</button>
                    ) : null}
                  </div>
                </>
              ) : null}

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
                        <small>{turn.role} · {turn.language ? languageName(turn.language) : 'language not reported by SDK'}</small>
                        {turn.text}
                      </div>
                    ))}
                    {liveTranscript ? (
                      <div className={`transcript-turn ${liveTranscript.role} ${styles.liveTurn}`} dir="auto">
                        <small>{liveTranscript.role} · live · {liveTranscript.language ? languageName(liveTranscript.language) : 'language not reported by SDK'}</small>
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
              <button type="button" className={styles.stageRetry} disabled={!browserTestAvailable || active} onClick={() => void startSession()}><RefreshCw size={12} /> Retry</button>
            </div>
          ) : null}

          <div className="call-controls">
            {phoneOnlySelected ? (
              phoneTestReady ? (
                <a className="call-button" href={`tel:${selectedPhoneNumber}`}><PhoneCall size={15} /> Call assigned number</a>
              ) : (
                <button className="call-button" disabled title={selected ? agentTestReadinessMessage(selected, selectedRuntimeProfile) : 'Select a voice agent before testing.'}><PhoneCall size={15} /> Phone test unavailable</button>
              )
            ) : !active ? (
              <>
                <button
                  className="call-button"
                  disabled={!browserTestAvailable || agentsLoading}
                  title={browserTestAvailable ? undefined : selected ? agentTestReadinessMessage(selected, selectedRuntimeProfile) : 'Select a voice agent before testing.'}
                  onClick={() => void startSession()}
                ><Play size={15} /> {state === 'error' || state === 'ended' ? 'Start new browser test' : 'Test in browser'}</button>
                {selectedUsesLiveKitBrowser ? phoneTestReady ? (
                  <a className={`call-button ${styles.phoneAction}`} href={`tel:${selectedPhoneNumber}`}><PhoneCall size={15} /> Call assigned number</a>
                ) : (
                  <button className={`call-button ${styles.phoneAction}`} disabled title="Assign and activate an e& phone number to test the carrier path."><PhoneCall size={15} /> Phone test unavailable</button>
                ) : null}
              </>
            ) : (
              <>
                <button className="icon-button" style={{ background: 'rgba(255,255,255,.09)', borderColor: 'rgba(255,255,255,.12)', color: 'white' }} onClick={() => void toggleMute()} aria-label={muted ? 'Unmute microphone' : 'Mute microphone'}>{muted ? <MicOff size={17} /> : <Mic size={17} />}</button>
                <button className="call-button end" onClick={endSession}><PhoneOff size={15} /> End browser test</button>
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
    initializing: 'Agent is getting ready…',
    listening: 'Listening',
    thinking: 'Agent is thinking',
    speaking: 'Agent is speaking',
    reconnecting: 'Reconnecting…',
    ended: 'Session complete',
    error: 'Connection issue',
  };
  return labels[state];
}

function sessionDescription(
  state: SessionState,
  selected: VoiceAgent | undefined,
  selectedReady: boolean,
  runtimeProfile?: RuntimeProfile,
  liveKitCandidate = false,
) {
  if (state === 'idle') {
    if (!selected) return 'Select a voice agent to begin';
    if (liveKitCandidate) return 'Start to run live credential, worker, knowledge, capacity, and Inworld checks';
    return selectedReady ? 'Start a private test conversation' : agentTestReadinessMessage(selected, runtimeProfile);
  }
  if (state === 'speaking') return 'Agent audio is streaming';
  if (state === 'thinking') return 'The agent is preparing a response';
  if (state === 'listening') return 'Speak naturally — interruption is supported';
  if (state === 'initializing') return 'The browser is connected and the agent worker is joining';
  if (state === 'reconnecting') return 'LiveKit is restoring the browser connection';
  if (state === 'connecting') return 'Preparing microphone, token, and secure connection';
  return 'Review the transcript and diagnostics before another test';
}

