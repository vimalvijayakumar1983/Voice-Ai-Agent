'use client';

import { useState, useMemo } from 'react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from 'recharts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { useApiQuery } from '@/hooks/use-api';

// ============================================================================
// Types
// ============================================================================

interface CallTrendsData {
  period: { from: string; to: string };
  volume: {
    total: number;
    completed: number;
    failed: number;
    noAnswer: number;
    byDay: Array<{ date: string; count: number }>;
  };
}

interface TimeRangeOption {
  label: string;
  value: string;
  days: number;
}

const TIME_RANGES: TimeRangeOption[] = [
  { label: '7d', value: '7d', days: 7 },
  { label: '30d', value: '30d', days: 30 },
  { label: '90d', value: '90d', days: 90 },
];

const GRANULARITIES = [
  { label: 'Hourly', value: 'hourly' },
  { label: 'Daily', value: 'daily' },
  { label: 'Weekly', value: 'weekly' },
] as const;

// ============================================================================
// Component
// ============================================================================

export function CallTrendsChart() {
  const [timeRange, setTimeRange] = useState<string>('30d');
  const [granularity, setGranularity] = useState<string>('daily');

  const selectedRange = TIME_RANGES.find((r) => r.value === timeRange) || TIME_RANGES[1];
  const from = useMemo(() => {
    const d = new Date();
    d.setDate(d.getDate() - selectedRange.days);
    return d.toISOString();
  }, [selectedRange.days]);

  const { data, isLoading } = useApiQuery<CallTrendsData>(
    ['analytics', 'call-trends', timeRange, granularity],
    `/analytics/calls/trends?from=${from}&granularity=${granularity}`,
  );

  // Build chart data with multi-series
  const chartData = useMemo(() => {
    if (!data?.volume?.byDay) {
      // Generate placeholder data
      const days = selectedRange.days;
      return Array.from({ length: Math.min(days, 30) }, (_, i) => {
        const d = new Date();
        d.setDate(d.getDate() - (days - i - 1));
        return {
          date: d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
          completed: Math.floor(Math.random() * 200) + 50,
          failed: Math.floor(Math.random() * 30) + 5,
          noAnswer: Math.floor(Math.random() * 50) + 10,
        };
      });
    }

    return data.volume.byDay.map((d) => ({
      date: new Date(d.date).toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
      }),
      completed: d.count,
      failed: 0,
      noAnswer: 0,
    }));
  }, [data, selectedRange.days]);

  return (
    <Card className="col-span-full">
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-base font-semibold">Call Volume Trends</CardTitle>
        <div className="flex items-center gap-3">
          {/* Legend */}
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            <span className="flex items-center gap-1.5">
              <span className="h-2.5 w-2.5 rounded-full bg-emerald-500" />
              Completed
            </span>
            <span className="flex items-center gap-1.5">
              <span className="h-2.5 w-2.5 rounded-full bg-red-500" />
              Failed
            </span>
            <span className="flex items-center gap-1.5">
              <span className="h-2.5 w-2.5 rounded-full bg-amber-500" />
              No Answer
            </span>
          </div>

          {/* Granularity */}
          <div className="flex items-center rounded-md border">
            {GRANULARITIES.map((g) => (
              <button
                key={g.value}
                onClick={() => setGranularity(g.value)}
                className={cn(
                  'px-2 py-1 text-[10px] font-medium transition-colors',
                  granularity === g.value
                    ? 'bg-primary text-primary-foreground'
                    : 'text-muted-foreground hover:text-foreground',
                )}
              >
                {g.label}
              </button>
            ))}
          </div>

          {/* Time range */}
          <div className="flex items-center rounded-md border">
            {TIME_RANGES.map((r) => (
              <button
                key={r.value}
                onClick={() => setTimeRange(r.value)}
                className={cn(
                  'px-2 py-1 text-[10px] font-medium transition-colors',
                  timeRange === r.value
                    ? 'bg-primary text-primary-foreground'
                    : 'text-muted-foreground hover:text-foreground',
                )}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>
      </CardHeader>
      <CardContent className="pt-4">
        <div className="h-[320px]">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
              <XAxis
                dataKey="date"
                tick={{ fontSize: 11 }}
                className="text-muted-foreground"
                tickLine={false}
                axisLine={false}
              />
              <YAxis
                tick={{ fontSize: 11 }}
                className="text-muted-foreground"
                tickLine={false}
                axisLine={false}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: 'hsl(var(--card))',
                  border: '1px solid hsl(var(--border))',
                  borderRadius: '8px',
                  fontSize: '12px',
                }}
              />
              <Line
                type="monotone"
                dataKey="completed"
                stroke="#10b981"
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4 }}
              />
              <Line
                type="monotone"
                dataKey="failed"
                stroke="#ef4444"
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4 }}
              />
              <Line
                type="monotone"
                dataKey="noAnswer"
                stroke="#f59e0b"
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4 }}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
  );
}
