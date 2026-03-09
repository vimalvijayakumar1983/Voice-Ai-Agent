'use client';

import { useEffect, useCallback, useState, useRef } from 'react';
import { useWebSocket } from '@/hooks/use-websocket';
import { useApiMutation, useApiQuery } from '@/hooks/use-api';

export type CallPhase =
  | 'idle'
  | 'initiating'
  | 'ringing'
  | 'answered'
  | 'listening'
  | 'speaking'
  | 'processing'
  | 'transferring'
  | 'ended';

interface TranscriptEntry {
  id: string;
  text: string;
  speaker: 'ai' | 'human';
  isFinal: boolean;
  timestamp: string;
  sentiment?: string;
}

interface CallMetrics {
  sttLatencyMs: number;
  llmLatencyMs: number;
  ttsLatencyMs: number;
  totalLatencyMs: number;
  turnCount: number;
}

interface CallSessionState {
  callId: string | null;
  phase: CallPhase;
  transcript: TranscriptEntry[];
  metrics: CallMetrics | null;
  startedAt: string | null;
  duration: number;
  isRecording: boolean;
  error: string | null;
}

interface UseCallSessionOptions {
  callId?: string;
  autoSubscribe?: boolean;
  maxTranscriptEntries?: number;
}

interface UseCallSessionReturn {
  state: CallSessionState;
  initiateCall: (params: {
    toNumber: string;
    agentId: string;
    contactId?: string;
    campaignId?: string;
  }) => Promise<void>;
  endCall: () => Promise<void>;
  isLoading: boolean;
  isInCall: boolean;
}

export function useCallSession(
  options: UseCallSessionOptions = {},
): UseCallSessionReturn {
  const {
    callId: initialCallId,
    autoSubscribe = true,
    maxTranscriptEntries = 200,
  } = options;

  const { emit, on, isConnected } = useWebSocket({ namespace: '/calls' });
  const durationIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const entryIdCounter = useRef(0);

  const [state, setState] = useState<CallSessionState>({
    callId: initialCallId || null,
    phase: initialCallId ? 'answered' : 'idle',
    transcript: [],
    metrics: null,
    startedAt: null,
    duration: 0,
    isRecording: false,
    error: null,
  });

  // Initiate call mutation
  const initiateCallMutation = useApiMutation<
    { id: string; status: string },
    { toNumber: string; agentId: string; contactId?: string; campaignId?: string }
  >('/calls/initiate', 'post', [['calls']]);

  // End call mutation
  const endCallMutation = useApiMutation<void, void>(
    state.callId ? `/calls/${state.callId}/end` : '/calls/end',
    'post',
    [['calls']],
  );

  // Subscribe to call events when we have a callId
  useEffect(() => {
    if (!state.callId || !autoSubscribe || !isConnected) return;

    emit('subscribe:call', { callId: state.callId });

    return () => {
      emit('unsubscribe:call', { callId: state.callId });
    };
  }, [state.callId, autoSubscribe, isConnected, emit]);

  // Listen for call events
  useEffect(() => {
    if (!isConnected) return;

    const unsubEvent = on('call:event', (data: unknown) => {
      const event = data as {
        callId: string;
        tenantId: string;
        type: string;
        data: Record<string, unknown>;
        timestamp: string;
      };

      if (event.callId !== state.callId) return;

      switch (event.type) {
        case 'status_change': {
          const newPhase = mapStatusToPhase(event.data.status as string);
          setState((prev) => ({ ...prev, phase: newPhase }));

          if (newPhase === 'ended') {
            stopDurationTimer();
          }
          break;
        }

        case 'transcript_update': {
          const entry: TranscriptEntry = {
            id: `t-${++entryIdCounter.current}`,
            text: event.data.content as string,
            speaker: event.data.role === 'assistant' ? 'ai' : 'human',
            isFinal: true,
            timestamp: event.timestamp,
            sentiment: event.data.sentiment as string | undefined,
          };

          setState((prev) => ({
            ...prev,
            transcript: [...prev.transcript, entry].slice(-maxTranscriptEntries),
          }));
          break;
        }

        case 'call_ended':
          setState((prev) => ({ ...prev, phase: 'ended' }));
          stopDurationTimer();
          break;

        case 'error':
          setState((prev) => ({
            ...prev,
            error: event.data.message as string || 'Unknown error occurred',
          }));
          break;
      }
    });

    // Also listen for specific transcript events from the monitor namespace
    const unsubTranscript = on('call:transcript', (data: unknown) => {
      const transcript = data as TranscriptEntry & { callId: string };
      if (transcript.callId !== state.callId) return;

      setState((prev) => {
        const existing = prev.transcript;

        // Replace the last interim transcript from the same speaker
        if (!transcript.isFinal) {
          const lastIdx = existing.length - 1;
          if (
            lastIdx >= 0 &&
            !existing[lastIdx].isFinal &&
            existing[lastIdx].speaker === transcript.speaker
          ) {
            const updated = [...existing];
            updated[lastIdx] = {
              ...transcript,
              id: existing[lastIdx].id,
            };
            return { ...prev, transcript: updated };
          }
        }

        const entry: TranscriptEntry = {
          id: `t-${++entryIdCounter.current}`,
          ...transcript,
        };

        return {
          ...prev,
          transcript: [...existing, entry].slice(-maxTranscriptEntries),
        };
      });
    });

    // Listen for phase changes
    const unsubPhase = on('call:phase', (data: unknown) => {
      const phaseData = data as { callId: string; phase: string };
      if (phaseData.callId !== state.callId) return;

      setState((prev) => ({
        ...prev,
        phase: phaseData.phase as CallPhase,
      }));
    });

    // Listen for metrics
    const unsubMetrics = on('call:metrics', (data: unknown) => {
      const metricsData = data as CallMetrics & { callId: string };
      if (metricsData.callId !== state.callId) return;

      setState((prev) => ({
        ...prev,
        metrics: {
          sttLatencyMs: metricsData.sttLatencyMs,
          llmLatencyMs: metricsData.llmLatencyMs,
          ttsLatencyMs: metricsData.ttsLatencyMs,
          totalLatencyMs: metricsData.totalLatencyMs,
          turnCount: metricsData.turnCount,
        },
      }));
    });

    return () => {
      if (unsubEvent) unsubEvent();
      if (unsubTranscript) unsubTranscript();
      if (unsubPhase) unsubPhase();
      if (unsubMetrics) unsubMetrics();
    };
  }, [isConnected, state.callId, on, maxTranscriptEntries]);

  // Duration timer
  const startDurationTimer = useCallback(() => {
    if (durationIntervalRef.current) return;

    durationIntervalRef.current = setInterval(() => {
      setState((prev) => {
        if (!prev.startedAt) return prev;
        const elapsed = Math.floor((Date.now() - new Date(prev.startedAt).getTime()) / 1000);
        return { ...prev, duration: elapsed };
      });
    }, 1000);
  }, []);

  const stopDurationTimer = useCallback(() => {
    if (durationIntervalRef.current) {
      clearInterval(durationIntervalRef.current);
      durationIntervalRef.current = null;
    }
  }, []);

  // Clean up timer on unmount
  useEffect(() => {
    return () => stopDurationTimer();
  }, [stopDurationTimer]);

  // ─── Public Methods ────────────────────────────────────────────────────────

  const initiateCall = useCallback(
    async (params: {
      toNumber: string;
      agentId: string;
      contactId?: string;
      campaignId?: string;
    }) => {
      setState((prev) => ({
        ...prev,
        phase: 'initiating',
        error: null,
        transcript: [],
        metrics: null,
      }));

      try {
        const result = await initiateCallMutation.mutateAsync(params);
        const callId = (result as any).id;

        setState((prev) => ({
          ...prev,
          callId,
          phase: 'ringing',
          startedAt: new Date().toISOString(),
          isRecording: true,
        }));

        startDurationTimer();

        // Subscribe to call events
        emit('subscribe:call', { callId });
      } catch (error) {
        setState((prev) => ({
          ...prev,
          phase: 'idle',
          error: (error as Error).message || 'Failed to initiate call',
        }));
      }
    },
    [initiateCallMutation, emit, startDurationTimer],
  );

  const endCall = useCallback(async () => {
    if (!state.callId) return;

    try {
      await endCallMutation.mutateAsync(undefined as never);
      setState((prev) => ({ ...prev, phase: 'ended' }));
      stopDurationTimer();
    } catch (error) {
      setState((prev) => ({
        ...prev,
        error: (error as Error).message || 'Failed to end call',
      }));
    }
  }, [state.callId, endCallMutation, stopDurationTimer]);

  const isLoading = initiateCallMutation.isPending || endCallMutation.isPending;
  const isInCall = state.phase !== 'idle' && state.phase !== 'ended';

  return {
    state,
    initiateCall,
    endCall,
    isLoading,
    isInCall,
  };
}

function mapStatusToPhase(status: string): CallPhase {
  const map: Record<string, CallPhase> = {
    queued: 'initiating',
    ringing: 'ringing',
    in_progress: 'answered',
    'in-progress': 'answered',
    listening: 'listening',
    speaking: 'speaking',
    processing: 'processing',
    transferring: 'transferring',
    completed: 'ended',
    failed: 'ended',
    busy: 'ended',
    no_answer: 'ended',
    cancelled: 'ended',
  };
  return map[status.toLowerCase()] || 'idle';
}
