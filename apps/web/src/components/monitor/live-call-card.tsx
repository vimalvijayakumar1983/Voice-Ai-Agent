'use client';

import { useState, useEffect, useCallback } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Phone,
  PhoneOff,
  Mic,
  MicOff,
  User,
  Bot,
  Clock,
  Activity,
  MessageSquare,
} from 'lucide-react';

interface TranscriptEntry {
  text: string;
  speaker: 'ai' | 'human';
  isFinal: boolean;
  timestamp: string;
  sentiment?: string;
}

interface LiveCallCardProps {
  callId: string;
  contactName?: string;
  contactPhone?: string;
  agentName?: string;
  phase: string;
  startedAt: string;
  sentiment?: string;
  transcriptEntries: TranscriptEntry[];
  onEndCall?: (callId: string) => void;
  onWhisper?: (callId: string, message: string) => void;
  onBargeIn?: (callId: string) => void;
}

const phaseColors: Record<string, string> = {
  initiated: 'bg-gray-500',
  ringing: 'bg-yellow-500',
  answered: 'bg-blue-500',
  listening: 'bg-green-500',
  speaking: 'bg-indigo-500',
  processing: 'bg-purple-500',
  transferring: 'bg-orange-500',
  ended: 'bg-gray-400',
};

const sentimentColors: Record<string, string> = {
  positive: 'border-l-green-500 bg-green-50 dark:bg-green-950/20',
  neutral: 'border-l-gray-300 bg-gray-50 dark:bg-gray-900/20',
  negative: 'border-l-red-500 bg-red-50 dark:bg-red-950/20',
};

function formatDuration(startedAt: string): string {
  const start = new Date(startedAt).getTime();
  const elapsed = Math.floor((Date.now() - start) / 1000);
  const minutes = Math.floor(elapsed / 60);
  const seconds = elapsed % 60;
  return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
}

export function LiveCallCard({
  callId,
  contactName,
  contactPhone,
  agentName,
  phase,
  startedAt,
  sentiment,
  transcriptEntries,
  onEndCall,
  onWhisper,
  onBargeIn,
}: LiveCallCardProps) {
  const [duration, setDuration] = useState(formatDuration(startedAt));
  const [isMuted, setIsMuted] = useState(false);
  const [whisperText, setWhisperText] = useState('');
  const [showWhisper, setShowWhisper] = useState(false);

  // Update duration every second
  useEffect(() => {
    if (phase === 'ended') return;

    const interval = setInterval(() => {
      setDuration(formatDuration(startedAt));
    }, 1000);

    return () => clearInterval(interval);
  }, [startedAt, phase]);

  const handleEndCall = useCallback(() => {
    if (onEndCall) onEndCall(callId);
  }, [callId, onEndCall]);

  const handleWhisper = useCallback(() => {
    if (onWhisper && whisperText.trim()) {
      onWhisper(callId, whisperText.trim());
      setWhisperText('');
      setShowWhisper(false);
    }
  }, [callId, whisperText, onWhisper]);

  const handleBargeIn = useCallback(() => {
    if (onBargeIn) onBargeIn(callId);
  }, [callId, onBargeIn]);

  const lastTranscript = transcriptEntries.length > 0
    ? transcriptEntries[transcriptEntries.length - 1]
    : null;

  const sentimentIndicator = sentiment || lastTranscript?.sentiment || 'neutral';

  return (
    <Card className={cn(
      'transition-all duration-300 border-l-4',
      sentimentColors[sentimentIndicator] || sentimentColors.neutral,
    )}>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className={cn(
              'h-3 w-3 rounded-full animate-pulse',
              phaseColors[phase] || 'bg-gray-400',
            )} />
            <CardTitle className="text-sm font-medium">
              {contactName || contactPhone || 'Unknown Contact'}
            </CardTitle>
            <Badge variant="outline" className="text-[10px]">
              {phase.toUpperCase()}
            </Badge>
          </div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Clock className="h-3 w-3" />
            <span className="font-mono">{duration}</span>
          </div>
        </div>

        {/* Contact & Agent info */}
        <div className="flex items-center gap-4 text-xs text-muted-foreground mt-1">
          {contactPhone && (
            <span className="flex items-center gap-1">
              <User className="h-3 w-3" />
              {contactPhone}
            </span>
          )}
          {agentName && (
            <span className="flex items-center gap-1">
              <Bot className="h-3 w-3" />
              {agentName}
            </span>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-3">
        {/* Live transcript preview */}
        <div className="rounded-md bg-muted/50 p-3 min-h-[60px] max-h-[120px] overflow-y-auto">
          {lastTranscript ? (
            <div className="space-y-1.5">
              {transcriptEntries.slice(-3).map((entry, idx) => (
                <div key={idx} className="flex items-start gap-2">
                  <span className={cn(
                    'shrink-0 mt-0.5 rounded-full p-0.5',
                    entry.speaker === 'ai'
                      ? 'bg-primary/10 text-primary'
                      : 'bg-orange-100 text-orange-600 dark:bg-orange-900/30 dark:text-orange-400',
                  )}>
                    {entry.speaker === 'ai' ? (
                      <Bot className="h-3 w-3" />
                    ) : (
                      <User className="h-3 w-3" />
                    )}
                  </span>
                  <p className={cn(
                    'text-xs leading-relaxed',
                    !entry.isFinal && 'text-muted-foreground italic',
                  )}>
                    {entry.text}
                  </p>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground text-center py-3">
              Waiting for conversation...
            </p>
          )}
        </div>

        {/* Whisper input */}
        {showWhisper && (
          <div className="flex gap-2">
            <input
              type="text"
              value={whisperText}
              onChange={(e) => setWhisperText(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleWhisper()}
              placeholder="Type whisper message..."
              className="flex-1 rounded-md border bg-background px-3 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
              autoFocus
            />
            <Button size="sm" variant="outline" onClick={handleWhisper} className="text-xs h-7">
              Send
            </Button>
          </div>
        )}

        {/* Control buttons */}
        {phase !== 'ended' && (
          <div className="flex items-center justify-between pt-1">
            <div className="flex items-center gap-1.5">
              <Button
                size="sm"
                variant="ghost"
                className="h-7 w-7 p-0"
                onClick={() => setIsMuted(!isMuted)}
                title={isMuted ? 'Unmute' : 'Mute'}
              >
                {isMuted ? <MicOff className="h-3.5 w-3.5" /> : <Mic className="h-3.5 w-3.5" />}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="h-7 w-7 p-0"
                onClick={() => setShowWhisper(!showWhisper)}
                title="Whisper"
              >
                <MessageSquare className="h-3.5 w-3.5" />
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="h-7 px-2 text-xs"
                onClick={handleBargeIn}
                title="Barge in"
              >
                <Activity className="h-3.5 w-3.5 mr-1" />
                Barge
              </Button>
            </div>
            <Button
              size="sm"
              variant="destructive"
              className="h-7 px-3 text-xs"
              onClick={handleEndCall}
            >
              <PhoneOff className="h-3.5 w-3.5 mr-1" />
              End
            </Button>
          </div>
        )}

        {/* Metrics bar */}
        <div className="flex items-center justify-between text-[10px] text-muted-foreground pt-1 border-t">
          <span>ID: {callId.slice(0, 8)}...</span>
          <span className="flex items-center gap-1">
            <Phone className="h-2.5 w-2.5" />
            {transcriptEntries.filter((e) => e.isFinal).length} turns
          </span>
        </div>
      </CardContent>
    </Card>
  );
}

function cn(...classes: (string | boolean | undefined)[]): string {
  return classes.filter(Boolean).join(' ');
}
