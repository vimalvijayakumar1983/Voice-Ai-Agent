'use client';

import { useState, useCallback, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Phone,
  PhoneOff,
  Mic,
  MicOff,
  Pause,
  Play,
  PhoneForwarded,
  Circle,
  Clock,
  Volume2,
  VolumeX,
} from 'lucide-react';
import { useApiMutation } from '@/hooks/use-api';

interface CallControlsProps {
  callId?: string;
  isInCall: boolean;
  phase?: string;
  startedAt?: string;
  isRecording?: boolean;
  onInitiateCall?: (data: { toNumber: string; agentId: string }) => void;
  onEndCall?: () => void;
  onMuteToggle?: (muted: boolean) => void;
  onHoldToggle?: (onHold: boolean) => void;
  onTransfer?: (targetNumber: string) => void;
  agents?: Array<{ id: string; name: string }>;
}

function formatTimer(startedAt: string): string {
  const start = new Date(startedAt).getTime();
  const elapsed = Math.floor((Date.now() - start) / 1000);
  const hours = Math.floor(elapsed / 3600);
  const minutes = Math.floor((elapsed % 3600) / 60);
  const seconds = elapsed % 60;

  if (hours > 0) {
    return `${hours}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
  }
  return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
}

export function CallControls({
  callId,
  isInCall,
  phase,
  startedAt,
  isRecording,
  onInitiateCall,
  onEndCall,
  onMuteToggle,
  onHoldToggle,
  onTransfer,
  agents = [],
}: CallControlsProps) {
  const [isMuted, setIsMuted] = useState(false);
  const [isOnHold, setIsOnHold] = useState(false);
  const [timer, setTimer] = useState('00:00');
  const [showDialer, setShowDialer] = useState(!isInCall);
  const [showTransfer, setShowTransfer] = useState(false);

  // Dialer state
  const [toNumber, setToNumber] = useState('');
  const [selectedAgentId, setSelectedAgentId] = useState(agents[0]?.id || '');
  const [transferNumber, setTransferNumber] = useState('');

  // API mutations
  const initiateCallMutation = useApiMutation<unknown, { toNumber: string; agentId: string }>(
    '/calls/initiate',
    'post',
    [['calls']],
  );

  // Timer
  useEffect(() => {
    if (!startedAt || !isInCall) {
      setTimer('00:00');
      return;
    }

    const interval = setInterval(() => {
      setTimer(formatTimer(startedAt));
    }, 1000);

    setTimer(formatTimer(startedAt));
    return () => clearInterval(interval);
  }, [startedAt, isInCall]);

  const handleInitiateCall = useCallback(async () => {
    if (!toNumber.trim() || !selectedAgentId) return;

    if (onInitiateCall) {
      onInitiateCall({ toNumber: toNumber.trim(), agentId: selectedAgentId });
    } else {
      await initiateCallMutation.mutateAsync({
        toNumber: toNumber.trim(),
        agentId: selectedAgentId,
      });
    }

    setShowDialer(false);
  }, [toNumber, selectedAgentId, onInitiateCall, initiateCallMutation]);

  const handleEndCall = useCallback(() => {
    if (onEndCall) onEndCall();
    setIsMuted(false);
    setIsOnHold(false);
    setShowDialer(true);
    setShowTransfer(false);
  }, [onEndCall]);

  const handleMuteToggle = useCallback(() => {
    const newMuted = !isMuted;
    setIsMuted(newMuted);
    if (onMuteToggle) onMuteToggle(newMuted);
  }, [isMuted, onMuteToggle]);

  const handleHoldToggle = useCallback(() => {
    const newHold = !isOnHold;
    setIsOnHold(newHold);
    if (onHoldToggle) onHoldToggle(newHold);
  }, [isOnHold, onHoldToggle]);

  const handleTransfer = useCallback(() => {
    if (transferNumber.trim() && onTransfer) {
      onTransfer(transferNumber.trim());
      setShowTransfer(false);
      setTransferNumber('');
    }
  }, [transferNumber, onTransfer]);

  // ─── DIALER VIEW ──────────────────────────────────────────────────────────────
  if (showDialer && !isInCall) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm flex items-center gap-2">
            <Phone className="h-4 w-4" />
            Initiate Call
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-2">
            <label className="text-xs font-medium text-muted-foreground">Phone Number</label>
            <input
              type="tel"
              value={toNumber}
              onChange={(e) => setToNumber(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleInitiateCall()}
              placeholder="+1 (555) 123-4567"
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>

          {agents.length > 0 && (
            <div className="space-y-2">
              <label className="text-xs font-medium text-muted-foreground">AI Agent</label>
              <select
                value={selectedAgentId}
                onChange={(e) => setSelectedAgentId(e.target.value)}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              >
                {agents.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.name}
                  </option>
                ))}
              </select>
            </div>
          )}

          <Button
            onClick={handleInitiateCall}
            disabled={!toNumber.trim() || initiateCallMutation.isPending}
            className="w-full gap-2"
          >
            <Phone className="h-4 w-4" />
            {initiateCallMutation.isPending ? 'Calling...' : 'Call'}
          </Button>
        </CardContent>
      </Card>
    );
  }

  // ─── IN-CALL CONTROLS ─────────────────────────────────────────────────────────
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <CardTitle className="text-sm">Call Controls</CardTitle>
            {phase && (
              <Badge
                variant={phase === 'speaking' ? 'default' : phase === 'listening' ? 'secondary' : 'outline'}
                className="text-[10px]"
              >
                {phase.toUpperCase()}
              </Badge>
            )}
          </div>

          <div className="flex items-center gap-2">
            {/* Recording indicator */}
            {isRecording && (
              <div className="flex items-center gap-1.5">
                <Circle className="h-2.5 w-2.5 fill-red-500 text-red-500 animate-pulse" />
                <span className="text-[10px] font-medium text-red-600 dark:text-red-400">REC</span>
              </div>
            )}

            {/* Timer */}
            <div className="flex items-center gap-1.5 text-sm font-mono">
              <Clock className="h-3.5 w-3.5 text-muted-foreground" />
              {timer}
            </div>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-3">
        {/* Transfer input */}
        {showTransfer && (
          <div className="flex gap-2">
            <input
              type="tel"
              value={transferNumber}
              onChange={(e) => setTransferNumber(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleTransfer()}
              placeholder="Transfer to number..."
              className="flex-1 rounded-md border bg-background px-3 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
              autoFocus
            />
            <Button size="sm" onClick={handleTransfer} className="text-xs h-7">
              Transfer
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setShowTransfer(false)}
              className="text-xs h-7"
            >
              Cancel
            </Button>
          </div>
        )}

        {/* Control buttons */}
        <div className="flex items-center justify-center gap-3">
          {/* Mute */}
          <Button
            size="icon"
            variant={isMuted ? 'destructive' : 'outline'}
            className="h-11 w-11 rounded-full"
            onClick={handleMuteToggle}
            title={isMuted ? 'Unmute' : 'Mute'}
          >
            {isMuted ? <MicOff className="h-4 w-4" /> : <Mic className="h-4 w-4" />}
          </Button>

          {/* Hold */}
          <Button
            size="icon"
            variant={isOnHold ? 'secondary' : 'outline'}
            className="h-11 w-11 rounded-full"
            onClick={handleHoldToggle}
            title={isOnHold ? 'Resume' : 'Hold'}
          >
            {isOnHold ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />}
          </Button>

          {/* End Call */}
          <Button
            size="icon"
            variant="destructive"
            className="h-14 w-14 rounded-full"
            onClick={handleEndCall}
            title="End Call"
          >
            <PhoneOff className="h-5 w-5" />
          </Button>

          {/* Transfer */}
          <Button
            size="icon"
            variant="outline"
            className="h-11 w-11 rounded-full"
            onClick={() => setShowTransfer(!showTransfer)}
            title="Transfer"
          >
            <PhoneForwarded className="h-4 w-4" />
          </Button>

          {/* Volume (placeholder) */}
          <Button
            size="icon"
            variant="outline"
            className="h-11 w-11 rounded-full"
            title="Volume"
          >
            <Volume2 className="h-4 w-4" />
          </Button>
        </div>

        {/* Call ID */}
        {callId && (
          <p className="text-center text-[10px] text-muted-foreground">
            Call ID: {callId}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
