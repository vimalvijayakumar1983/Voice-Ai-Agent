'use client';

import { useEffect, useRef } from 'react';
import { Bot, User } from 'lucide-react';

interface TranscriptEntry {
  id: string;
  text: string;
  speaker: 'ai' | 'human';
  isFinal: boolean;
  timestamp: string;
  sentiment?: 'positive' | 'neutral' | 'negative';
}

interface LiveTranscriptStreamProps {
  entries: TranscriptEntry[];
  autoScroll?: boolean;
  showTimestamps?: boolean;
  highlightSentiment?: boolean;
  maxHeight?: string;
}

const sentimentStyles: Record<string, string> = {
  positive: 'border-l-green-500',
  neutral: 'border-l-transparent',
  negative: 'border-l-red-500',
};

const sentimentBadgeStyles: Record<string, string> = {
  positive: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  negative: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
};

function formatTimestamp(timestamp: string): string {
  try {
    const date = new Date(timestamp);
    return date.toLocaleTimeString('en-US', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    });
  } catch {
    return '';
  }
}

export function LiveTranscriptStream({
  entries,
  autoScroll = true,
  showTimestamps = true,
  highlightSentiment = true,
  maxHeight = '500px',
}: LiveTranscriptStreamProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom when new entries arrive
  useEffect(() => {
    if (autoScroll && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [entries, autoScroll]);

  if (entries.length === 0) {
    return (
      <div
        className="flex items-center justify-center rounded-lg border border-dashed p-8"
        style={{ maxHeight }}
      >
        <div className="text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-muted">
            <Bot className="h-6 w-6 text-muted-foreground" />
          </div>
          <p className="text-sm font-medium text-muted-foreground">
            Waiting for conversation to begin...
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            Live transcript will appear here
          </p>
        </div>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="space-y-2 overflow-y-auto rounded-lg border bg-card p-4"
      style={{ maxHeight }}
    >
      {entries.map((entry, index) => {
        const isAI = entry.speaker === 'ai';
        const isInterim = !entry.isFinal;
        const showSentiment = highlightSentiment && entry.sentiment && entry.sentiment !== 'neutral';

        return (
          <div
            key={entry.id || index}
            className={cn(
              'flex gap-3 rounded-lg border-l-2 p-3 transition-all duration-200',
              isInterim ? 'opacity-70' : 'opacity-100',
              highlightSentiment && entry.sentiment
                ? sentimentStyles[entry.sentiment]
                : 'border-l-transparent',
              isInterim && 'bg-muted/30',
            )}
          >
            {/* Speaker avatar */}
            <div
              className={cn(
                'flex h-7 w-7 shrink-0 items-center justify-center rounded-full',
                isAI
                  ? 'bg-primary/10 text-primary'
                  : 'bg-orange-100 text-orange-600 dark:bg-orange-900/30 dark:text-orange-400',
              )}
            >
              {isAI ? <Bot className="h-3.5 w-3.5" /> : <User className="h-3.5 w-3.5" />}
            </div>

            {/* Content */}
            <div className="flex-1 min-w-0">
              <div className="mb-0.5 flex items-center gap-2">
                <span className="text-[11px] font-semibold">
                  {isAI ? 'AI Agent' : 'Contact'}
                </span>
                {showTimestamps && entry.timestamp && (
                  <span className="text-[10px] text-muted-foreground">
                    {formatTimestamp(entry.timestamp)}
                  </span>
                )}
                {isInterim && (
                  <span className="text-[9px] italic text-muted-foreground">
                    (transcribing...)
                  </span>
                )}
                {showSentiment && entry.sentiment && (
                  <span
                    className={cn(
                      'rounded-full px-1.5 py-0.5 text-[9px] font-medium',
                      sentimentBadgeStyles[entry.sentiment] || '',
                    )}
                  >
                    {entry.sentiment}
                  </span>
                )}
              </div>
              <p
                className={cn(
                  'text-sm leading-relaxed',
                  isInterim
                    ? 'text-muted-foreground italic'
                    : 'text-foreground/90',
                )}
              >
                {entry.text}
              </p>
            </div>
          </div>
        );
      })}

      {/* Scroll anchor */}
      <div ref={bottomRef} />
    </div>
  );
}

function cn(...classes: (string | boolean | undefined)[]): string {
  return classes.filter(Boolean).join(' ');
}
