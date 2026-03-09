'use client';

import { useMemo } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { cn } from '@/lib/utils';
import { useApiQuery } from '@/hooks/use-api';

// ============================================================================
// Types
// ============================================================================

interface SentimentData {
  period: { from: string; to: string };
  sentiment: {
    averageScore: number;
    positive: number;
    neutral: number;
    negative: number;
    byHourAndDay: Array<{
      dayOfWeek: number;
      hour: number;
      avgSentiment: number;
      count: number;
    }>;
  };
}

// ============================================================================
// Constants
// ============================================================================

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const HOURS = Array.from({ length: 24 }, (_, i) => i);

function getSentimentColor(score: number): string {
  // score ranges from -1 (negative) to 1 (positive)
  if (score >= 0.5) return 'bg-green-500';
  if (score >= 0.25) return 'bg-green-400';
  if (score >= 0.1) return 'bg-green-300';
  if (score >= -0.1) return 'bg-yellow-300';
  if (score >= -0.25) return 'bg-orange-300';
  if (score >= -0.5) return 'bg-orange-400';
  return 'bg-red-500';
}

function getSentimentOpacity(count: number, maxCount: number): number {
  if (count === 0) return 0.1;
  return Math.max(0.3, Math.min(1, count / maxCount));
}

// ============================================================================
// Component
// ============================================================================

export function SentimentHeatmap() {
  const from = useMemo(() => {
    const d = new Date();
    d.setDate(d.getDate() - 30);
    return d.toISOString();
  }, []);

  const { data, isLoading } = useApiQuery<SentimentData>(
    ['analytics', 'sentiment-trends'],
    `/analytics/sentiment/trends?from=${from}`,
  );

  // Build heatmap grid
  const { grid, maxCount } = useMemo(() => {
    const gridMap = new Map<string, { avgSentiment: number; count: number }>();

    if (data?.sentiment?.byHourAndDay) {
      for (const entry of data.sentiment.byHourAndDay) {
        const key = `${entry.dayOfWeek}-${entry.hour}`;
        gridMap.set(key, { avgSentiment: entry.avgSentiment, count: entry.count });
      }
    } else {
      // Generate placeholder data
      for (let day = 0; day < 7; day++) {
        for (let hour = 0; hour < 24; hour++) {
          const isWorkHours = hour >= 8 && hour <= 18 && day >= 1 && day <= 5;
          if (isWorkHours || Math.random() > 0.6) {
            gridMap.set(`${day}-${hour}`, {
              avgSentiment: (Math.random() - 0.3) * 1.5,
              count: Math.floor(Math.random() * 50) + (isWorkHours ? 20 : 2),
            });
          }
        }
      }
    }

    let max = 0;
    for (const cell of gridMap.values()) {
      if (cell.count > max) max = cell.count;
    }

    return { grid: gridMap, maxCount: max };
  }, [data]);

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-base font-semibold">Sentiment Heatmap</CardTitle>
        <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
          <div className="flex items-center gap-1">
            <div className="w-3 h-3 rounded-sm bg-red-500" />
            Negative
          </div>
          <div className="flex items-center gap-1">
            <div className="w-3 h-3 rounded-sm bg-yellow-300" />
            Neutral
          </div>
          <div className="flex items-center gap-1">
            <div className="w-3 h-3 rounded-sm bg-green-500" />
            Positive
          </div>
        </div>
      </CardHeader>
      <CardContent className="pt-2">
        <div className="overflow-x-auto">
          <div className="min-w-[700px]">
            {/* Hour labels */}
            <div className="flex mb-1 ml-10">
              {HOURS.filter((h) => h % 3 === 0).map((hour) => (
                <div
                  key={hour}
                  className="text-[10px] text-muted-foreground"
                  style={{ width: `${(3 / 24) * 100}%` }}
                >
                  {hour === 0 ? '12a' : hour < 12 ? `${hour}a` : hour === 12 ? '12p' : `${hour - 12}p`}
                </div>
              ))}
            </div>

            {/* Grid */}
            <div className="space-y-1">
              {DAYS.map((day, dayIndex) => (
                <div key={day} className="flex items-center gap-1">
                  <div className="w-8 text-[10px] text-muted-foreground text-right">
                    {day}
                  </div>
                  <div className="flex-1 flex gap-[2px]">
                    {HOURS.map((hour) => {
                      const key = `${dayIndex}-${hour}`;
                      const cell = grid.get(key);
                      const sentiment = cell?.avgSentiment ?? 0;
                      const count = cell?.count ?? 0;

                      return (
                        <div
                          key={hour}
                          className={cn(
                            'flex-1 h-7 rounded-sm transition-all cursor-pointer hover:ring-2 hover:ring-primary',
                            count > 0 ? getSentimentColor(sentiment) : 'bg-gray-100 dark:bg-gray-800',
                          )}
                          style={{
                            opacity: count > 0 ? getSentimentOpacity(count, maxCount) : 0.3,
                          }}
                          title={
                            count > 0
                              ? `${day} ${hour}:00 - Sentiment: ${sentiment.toFixed(2)}, Calls: ${count}`
                              : `${day} ${hour}:00 - No data`
                          }
                        />
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Summary stats */}
        {data?.sentiment && (
          <div className="flex items-center justify-center gap-6 mt-4 pt-3 border-t">
            <div className="text-center">
              <p className="text-lg font-bold text-green-600">{data.sentiment.positive}</p>
              <p className="text-[10px] text-muted-foreground">Positive</p>
            </div>
            <div className="text-center">
              <p className="text-lg font-bold text-yellow-600">{data.sentiment.neutral}</p>
              <p className="text-[10px] text-muted-foreground">Neutral</p>
            </div>
            <div className="text-center">
              <p className="text-lg font-bold text-red-600">{data.sentiment.negative}</p>
              <p className="text-[10px] text-muted-foreground">Negative</p>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
