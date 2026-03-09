'use client';

import { useState, useEffect, useCallback } from 'react';
import { Phone, Activity, Clock, CheckCircle2, Loader2, Users } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { cn } from '@/lib/utils';
import { useWebSocket } from '@/hooks/use-websocket';
import { useApiQuery } from '@/hooks/use-api';

// ============================================================================
// Types
// ============================================================================

interface RealtimeData {
  activeCalls: number;
  callsPerMinute: number;
  avgWaitTime: number;
  successRateLast15Min: number;
  queuedCalls: number;
  agentsActive: number;
  timestamp: string;
}

interface SparklinePoint {
  value: number;
  timestamp: number;
}

// ============================================================================
// Sparkline Component
// ============================================================================

function Sparkline({
  data,
  color,
  height = 32,
  width = 100,
}: {
  data: SparklinePoint[];
  color: string;
  height?: number;
  width?: number;
}) {
  if (data.length < 2) return null;

  const values = data.map((d) => d.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;

  const points = data
    .map((d, i) => {
      const x = (i / (data.length - 1)) * width;
      const y = height - ((d.value - min) / range) * (height - 4) - 2;
      return `${x},${y}`;
    })
    .join(' ');

  return (
    <svg width={width} height={height} className="overflow-visible">
      <polyline
        points={points}
        fill="none"
        stroke={color}
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// ============================================================================
// Metric Card
// ============================================================================

interface MetricCardProps {
  title: string;
  value: string | number;
  icon: React.ElementType;
  iconColor: string;
  iconBg: string;
  sparkData: SparklinePoint[];
  sparkColor: string;
  pulse?: boolean;
}

function MetricCard({
  title,
  value,
  icon: Icon,
  iconColor,
  iconBg,
  sparkData,
  sparkColor,
  pulse,
}: MetricCardProps) {
  return (
    <Card className="transition-shadow hover:shadow-md">
      <CardContent className="p-4">
        <div className="flex items-start justify-between">
          <div className="space-y-1">
            <p className="text-xs font-medium text-muted-foreground">{title}</p>
            <p className="text-2xl font-bold tracking-tight">{value}</p>
          </div>
          <div className={cn('rounded-lg p-2', iconBg)}>
            <Icon
              className={cn(
                'h-4 w-4',
                iconColor,
                pulse && 'animate-pulse',
              )}
            />
          </div>
        </div>
        <div className="mt-2">
          <Sparkline data={sparkData} color={sparkColor} height={28} width={120} />
        </div>
      </CardContent>
    </Card>
  );
}

// ============================================================================
// Component
// ============================================================================

export function RealtimeMetrics() {
  const [history, setHistory] = useState<RealtimeData[]>([]);
  const maxHistory = 30; // Keep last 30 snapshots

  // Fetch initial data via REST
  const { data: initialData } = useApiQuery<RealtimeData>(
    ['analytics', 'realtime'],
    '/analytics/realtime',
    { refetchInterval: 15000 }, // Poll every 15 seconds
  );

  // WebSocket for live updates
  const { on, isConnected } = useWebSocket({ namespace: '/analytics' });

  useEffect(() => {
    if (!initialData) return;
    setHistory((prev) => {
      const newHistory = [...prev, initialData].slice(-maxHistory);
      return newHistory;
    });
  }, [initialData]);

  useEffect(() => {
    const unsubscribe = on('realtime-update', (data: unknown) => {
      const metrics = data as RealtimeData;
      setHistory((prev) => [...prev, metrics].slice(-maxHistory));
    });
    return unsubscribe;
  }, [on]);

  const current = history.length > 0 ? history[history.length - 1] : null;

  const buildSparkData = useCallback(
    (key: keyof RealtimeData): SparklinePoint[] => {
      return history.map((h, i) => ({
        value: Number(h[key]) || 0,
        timestamp: i,
      }));
    },
    [history],
  );

  if (!current) {
    return (
      <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-4">
        {Array.from({ length: 6 }).map((_, i) => (
          <Card key={i}>
            <CardContent className="p-4 flex items-center justify-center h-28">
              <Loader2 className="w-5 h-5 animate-spin text-muted-foreground" />
            </CardContent>
          </Card>
        ))}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <div
          className={cn(
            'w-2 h-2 rounded-full',
            isConnected ? 'bg-green-500 animate-pulse' : 'bg-gray-400',
          )}
        />
        <span className="text-xs text-muted-foreground">
          {isConnected ? 'Live' : 'Polling'} - Last updated:{' '}
          {new Date(current.timestamp).toLocaleTimeString()}
        </span>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-4">
        <MetricCard
          title="Active Calls"
          value={current.activeCalls}
          icon={Phone}
          iconColor="text-green-600"
          iconBg="bg-green-100 dark:bg-green-900/30"
          sparkData={buildSparkData('activeCalls')}
          sparkColor="#16a34a"
          pulse={current.activeCalls > 0}
        />

        <MetricCard
          title="Calls/Minute"
          value={current.callsPerMinute}
          icon={Activity}
          iconColor="text-blue-600"
          iconBg="bg-blue-100 dark:bg-blue-900/30"
          sparkData={buildSparkData('callsPerMinute')}
          sparkColor="#2563eb"
        />

        <MetricCard
          title="Success Rate (15m)"
          value={`${current.successRateLast15Min}%`}
          icon={CheckCircle2}
          iconColor="text-emerald-600"
          iconBg="bg-emerald-100 dark:bg-emerald-900/30"
          sparkData={buildSparkData('successRateLast15Min')}
          sparkColor="#059669"
        />

        <MetricCard
          title="Avg Wait Time"
          value={`${Math.round(current.avgWaitTime)}s`}
          icon={Clock}
          iconColor="text-amber-600"
          iconBg="bg-amber-100 dark:bg-amber-900/30"
          sparkData={buildSparkData('avgWaitTime')}
          sparkColor="#d97706"
        />

        <MetricCard
          title="Queued Calls"
          value={current.queuedCalls}
          icon={Loader2}
          iconColor="text-purple-600"
          iconBg="bg-purple-100 dark:bg-purple-900/30"
          sparkData={buildSparkData('queuedCalls')}
          sparkColor="#9333ea"
        />

        <MetricCard
          title="Active Agents"
          value={current.agentsActive}
          icon={Users}
          iconColor="text-indigo-600"
          iconBg="bg-indigo-100 dark:bg-indigo-900/30"
          sparkData={buildSparkData('agentsActive')}
          sparkColor="#4f46e5"
        />
      </div>
    </div>
  );
}
