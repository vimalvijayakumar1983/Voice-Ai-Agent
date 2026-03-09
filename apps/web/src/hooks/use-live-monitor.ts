'use client';

import { useEffect, useRef, useCallback, useState, useMemo } from 'react';
import { io, type Socket } from 'socket.io-client';
import { useAuthStore } from '@/stores/auth-store';

interface ActiveCall {
  callId: string;
  tenantId: string;
  agentId: string;
  contactId?: string;
  campaignId?: string;
  phase: string;
  startedAt: string;
  toNumber?: string;
  contactName?: string;
  duration: number;
}

interface TranscriptEntry {
  callId: string;
  text: string;
  speaker: 'ai' | 'human';
  isFinal: boolean;
  timestamp: string;
  sentiment?: string;
}

interface CampaignProgress {
  campaignId: string;
  totalContacts: number;
  contactsProcessed: number;
  activeCalls: number;
  completedCalls: number;
  failedCalls: number;
  successRate: number;
  callsPerMinute: number;
}

interface CallMetrics {
  callId: string;
  phase: string;
  duration: number;
  sttLatencyMs: number;
  llmLatencyMs: number;
  ttsLatencyMs: number;
  turnCount: number;
}

interface UseLiveMonitorOptions {
  autoConnect?: boolean;
  maxTranscriptEntries?: number;
  maxActiveCalls?: number;
}

interface UseLiveMonitorReturn {
  isConnected: boolean;
  activeCalls: ActiveCall[];
  transcripts: Map<string, TranscriptEntry[]>;
  campaignProgress: Map<string, CampaignProgress>;
  callMetrics: Map<string, CallMetrics>;
  subscribeToCall: (callId: string) => void;
  unsubscribeFromCall: (callId: string) => void;
  subscribeToCampaign: (campaignId: string) => void;
  unsubscribeFromCampaign: (campaignId: string) => void;
  refreshActiveCalls: () => void;
  requestCampaignMetrics: (campaignId: string) => void;
  getCallTranscript: (callId: string) => TranscriptEntry[];
}

const MAX_RECONNECT_ATTEMPTS = 10;
const BASE_RECONNECT_DELAY = 1000;
const MAX_RECONNECT_DELAY = 30000;

export function useLiveMonitor(
  options: UseLiveMonitorOptions = {},
): UseLiveMonitorReturn {
  const {
    autoConnect = true,
    maxTranscriptEntries = 500,
    maxActiveCalls = 100,
  } = options;

  const { token } = useAuthStore();
  const socketRef = useRef<Socket | null>(null);
  const reconnectAttempts = useRef(0);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [isConnected, setIsConnected] = useState(false);
  const [activeCalls, setActiveCalls] = useState<ActiveCall[]>([]);
  const [transcripts, setTranscripts] = useState<Map<string, TranscriptEntry[]>>(new Map());
  const [campaignProgress, setCampaignProgress] = useState<Map<string, CampaignProgress>>(new Map());
  const [callMetrics, setCallMetrics] = useState<Map<string, CallMetrics>>(new Map());

  // Connect to the monitor WebSocket namespace
  useEffect(() => {
    if (!autoConnect || !token) return;

    const url = `${process.env.NEXT_PUBLIC_WS_URL || 'http://localhost:3001'}/monitor`;

    const socket = io(url, {
      auth: { token },
      transports: ['websocket', 'polling'],
      reconnection: false, // We handle reconnection manually for exponential backoff
    });

    socketRef.current = socket;

    socket.on('connect', () => {
      setIsConnected(true);
      reconnectAttempts.current = 0;

      // Request initial active calls data
      socket.emit('request:active-calls');
    });

    socket.on('disconnect', (reason) => {
      setIsConnected(false);

      // Attempt reconnection with exponential backoff
      if (reason !== 'io client disconnect') {
        scheduleReconnect();
      }
    });

    socket.on('connect_error', () => {
      setIsConnected(false);
      scheduleReconnect();
    });

    // ─── Event Handlers ──────────────────────────────────────────────────────

    socket.on('active-calls', (calls: ActiveCall[]) => {
      setActiveCalls(calls.slice(0, maxActiveCalls));
    });

    socket.on('call:started', (call: ActiveCall) => {
      setActiveCalls((prev) => {
        const updated = [call, ...prev].slice(0, maxActiveCalls);
        return updated;
      });
    });

    socket.on('call:ended', (data: { callId: string }) => {
      setActiveCalls((prev) => prev.filter((c) => c.callId !== data.callId));

      // Keep transcript data for a short period after call ends
      // It will be cleaned up when the component unmounts or entries exceed max
    });

    socket.on('call:phase', (data: { callId: string; phase: string }) => {
      setActiveCalls((prev) =>
        prev.map((c) =>
          c.callId === data.callId ? { ...c, phase: data.phase } : c,
        ),
      );
    });

    socket.on('call:transcript', (entry: TranscriptEntry) => {
      setTranscripts((prev) => {
        const next = new Map(prev);
        const existing = next.get(entry.callId) || [];

        // If this is an interim transcript, replace the last interim from the same speaker
        if (!entry.isFinal) {
          const lastIdx = existing.length - 1;
          if (lastIdx >= 0 && !existing[lastIdx].isFinal && existing[lastIdx].speaker === entry.speaker) {
            existing[lastIdx] = entry;
            next.set(entry.callId, [...existing]);
            return next;
          }
        }

        const updated = [...existing, entry].slice(-maxTranscriptEntries);
        next.set(entry.callId, updated);
        return next;
      });
    });

    socket.on('call:metrics', (metrics: CallMetrics) => {
      setCallMetrics((prev) => {
        const next = new Map(prev);
        next.set(metrics.callId, metrics);
        return next;
      });
    });

    socket.on('campaign:progress', (progress: CampaignProgress) => {
      setCampaignProgress((prev) => {
        const next = new Map(prev);
        next.set(progress.campaignId, progress);
        return next;
      });
    });

    socket.on('campaign:metrics', (data: CampaignProgress & { campaignId: string }) => {
      setCampaignProgress((prev) => {
        const next = new Map(prev);
        next.set(data.campaignId, data);
        return next;
      });
    });

    return () => {
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current);
      }
      socket.disconnect();
      socketRef.current = null;
      setIsConnected(false);
    };
  }, [token, autoConnect, maxTranscriptEntries, maxActiveCalls]);

  // ─── Reconnection with Exponential Backoff ──────────────────────────────────

  const scheduleReconnect = useCallback(() => {
    if (reconnectAttempts.current >= MAX_RECONNECT_ATTEMPTS) return;

    const delay = Math.min(
      BASE_RECONNECT_DELAY * Math.pow(2, reconnectAttempts.current),
      MAX_RECONNECT_DELAY,
    );

    reconnectAttempts.current++;

    reconnectTimer.current = setTimeout(() => {
      if (socketRef.current && !socketRef.current.connected) {
        socketRef.current.connect();
      }
    }, delay);
  }, []);

  // ─── Subscription Methods ───────────────────────────────────────────────────

  const subscribeToCall = useCallback((callId: string) => {
    socketRef.current?.emit('subscribe:call', { callId });
  }, []);

  const unsubscribeFromCall = useCallback((callId: string) => {
    socketRef.current?.emit('unsubscribe:call', { callId });
  }, []);

  const subscribeToCampaign = useCallback((campaignId: string) => {
    socketRef.current?.emit('subscribe:campaign', { campaignId });
  }, []);

  const unsubscribeFromCampaign = useCallback((campaignId: string) => {
    socketRef.current?.emit('unsubscribe:campaign', { campaignId });
  }, []);

  const refreshActiveCalls = useCallback(() => {
    socketRef.current?.emit('request:active-calls');
  }, []);

  const requestCampaignMetrics = useCallback((campaignId: string) => {
    socketRef.current?.emit('request:campaign-metrics', { campaignId });
  }, []);

  const getCallTranscript = useCallback(
    (callId: string): TranscriptEntry[] => {
      return transcripts.get(callId) || [];
    },
    [transcripts],
  );

  return {
    isConnected,
    activeCalls,
    transcripts,
    campaignProgress,
    callMetrics,
    subscribeToCall,
    unsubscribeFromCall,
    subscribeToCampaign,
    unsubscribeFromCampaign,
    refreshActiveCalls,
    requestCampaignMetrics,
    getCallTranscript,
  };
}
