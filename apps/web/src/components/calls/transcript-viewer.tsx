'use client';

import { cn } from '@/lib/utils';
import { Bot, User } from 'lucide-react';

interface TranscriptEntry {
  id: string;
  speaker: 'agent' | 'contact';
  text: string;
  timestamp: string;
  sentiment?: 'positive' | 'neutral' | 'negative';
}

interface TranscriptViewerProps {
  entries: TranscriptEntry[];
}

const sentimentColors = {
  positive: 'border-l-green-500',
  neutral: 'border-l-gray-300',
  negative: 'border-l-red-500',
};

export function TranscriptViewer({ entries }: TranscriptViewerProps) {
  return (
    <div className="space-y-3">
      {entries.map((entry) => (
        <div
          key={entry.id}
          className={cn(
            'flex gap-3 rounded-lg border-l-2 p-3 transition-colors hover:bg-muted/50',
            entry.sentiment ? sentimentColors[entry.sentiment] : 'border-l-transparent'
          )}
        >
          <div
            className={cn(
              'flex h-8 w-8 shrink-0 items-center justify-center rounded-full',
              entry.speaker === 'agent'
                ? 'bg-primary/10 text-primary'
                : 'bg-orange-100 text-orange-600 dark:bg-orange-900/30 dark:text-orange-400'
            )}
          >
            {entry.speaker === 'agent' ? (
              <Bot className="h-4 w-4" />
            ) : (
              <User className="h-4 w-4" />
            )}
          </div>
          <div className="flex-1">
            <div className="mb-1 flex items-center gap-2">
              <span className="text-xs font-semibold">
                {entry.speaker === 'agent' ? 'AI Agent' : 'Contact'}
              </span>
              <span className="text-[10px] text-muted-foreground">{entry.timestamp}</span>
              {entry.sentiment && entry.sentiment !== 'neutral' && (
                <span
                  className={cn(
                    'rounded-full px-1.5 py-0.5 text-[9px] font-medium',
                    entry.sentiment === 'positive'
                      ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                      : 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
                  )}
                >
                  {entry.sentiment}
                </span>
              )}
            </div>
            <p className="text-sm leading-relaxed text-foreground/90">{entry.text}</p>
          </div>
        </div>
      ))}
    </div>
  );
}
