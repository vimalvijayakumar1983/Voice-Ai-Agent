'use client';

import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Play, Pause, SkipBack, SkipForward, Volume2, VolumeX, Download } from 'lucide-react';

interface CallPlayerProps {
  recordingUrl?: string;
  duration: string;
}

export function CallPlayer({ recordingUrl, duration }: CallPlayerProps) {
  const [isPlaying, setIsPlaying] = useState(false);
  const [isMuted, setIsMuted] = useState(false);
  const [progress, setProgress] = useState(0);

  return (
    <Card>
      <CardContent className="p-4">
        <div className="space-y-3">
          {/* Waveform placeholder */}
          <div className="relative h-16 rounded-md bg-muted/50 overflow-hidden">
            <div className="absolute inset-0 flex items-center justify-center gap-[2px]">
              {Array.from({ length: 80 }).map((_, i) => {
                const height = Math.random() * 100;
                return (
                  <div
                    key={i}
                    className={cn(
                      'w-[2px] rounded-full transition-colors',
                      i / 80 * 100 < progress ? 'bg-primary' : 'bg-muted-foreground/20'
                    )}
                    style={{ height: `${Math.max(10, height)}%` }}
                  />
                );
              })}
            </div>
            {/* Progress overlay */}
            <div
              className="absolute inset-y-0 left-0 bg-primary/5"
              style={{ width: `${progress}%` }}
            />
          </div>

          {/* Controls */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Button variant="ghost" size="icon" className="h-8 w-8">
                <SkipBack className="h-4 w-4" />
              </Button>
              <Button
                size="icon"
                className="h-10 w-10 rounded-full"
                onClick={() => setIsPlaying(!isPlaying)}
              >
                {isPlaying ? (
                  <Pause className="h-4 w-4" />
                ) : (
                  <Play className="h-4 w-4 ml-0.5" />
                )}
              </Button>
              <Button variant="ghost" size="icon" className="h-8 w-8">
                <SkipForward className="h-4 w-4" />
              </Button>
            </div>

            <div className="flex items-center gap-3">
              <span className="font-mono text-xs text-muted-foreground">
                0:00 / {duration}
              </span>
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8"
                onClick={() => setIsMuted(!isMuted)}
              >
                {isMuted ? (
                  <VolumeX className="h-4 w-4" />
                ) : (
                  <Volume2 className="h-4 w-4" />
                )}
              </Button>
              <Button variant="ghost" size="icon" className="h-8 w-8">
                <Download className="h-4 w-4" />
              </Button>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function cn(...classes: (string | boolean | undefined)[]) {
  return classes.filter(Boolean).join(' ');
}
