'use client';

import { useState, useEffect, useCallback } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Play,
  Pause,
  Square,
  RefreshCw,
  Phone,
  PhoneCall,
  CheckCircle,
  XCircle,
  Clock,
  TrendingUp,
  Users,
  Activity,
  BarChart3,
} from 'lucide-react';
import { useApiMutation } from '@/hooks/use-api';
import { useWebSocket } from '@/hooks/use-websocket';

interface CampaignMetrics {
  totalContacts: number;
  contactsProcessed: number;
  contactsRemaining: number;
  activeCalls: number;
  completedCalls: number;
  failedCalls: number;
  busyCalls: number;
  noAnswerCalls: number;
  connectedCalls: number;
  totalRetries: number;
  successRate: number;
  callsPerMinute: number;
  averageDurationSeconds: number;
  startedAt: string;
  lastActivityAt: string;
}

interface ActiveCall {
  callId: string;
  contactName?: string;
  contactPhone?: string;
  phase: string;
  duration: number;
}

interface CampaignExecutionPanelProps {
  campaignId: string;
  campaignName: string;
  status: string;
  initialMetrics?: CampaignMetrics;
}

function formatDuration(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

function formatElapsed(startedAt: string): string {
  const start = new Date(startedAt).getTime();
  const elapsed = Math.floor((Date.now() - start) / 1000);
  const hours = Math.floor(elapsed / 3600);
  const minutes = Math.floor((elapsed % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

export function CampaignExecutionPanel({
  campaignId,
  campaignName,
  status: initialStatus,
  initialMetrics,
}: CampaignExecutionPanelProps) {
  const [metrics, setMetrics] = useState<CampaignMetrics | null>(initialMetrics || null);
  const [status, setStatus] = useState(initialStatus);
  const [activeCalls, setActiveCalls] = useState<ActiveCall[]>([]);
  const [elapsed, setElapsed] = useState('0m');

  const { emit, on } = useWebSocket({ namespace: '/monitor' });

  // Mutations for campaign control
  const startMutation = useApiMutation<unknown, void>(
    `/campaigns/${campaignId}/start`,
    'post',
    [['campaigns', campaignId]],
  );

  const pauseMutation = useApiMutation<unknown, void>(
    `/campaigns/${campaignId}/pause`,
    'post',
    [['campaigns', campaignId]],
  );

  const resumeMutation = useApiMutation<unknown, void>(
    `/campaigns/${campaignId}/resume`,
    'post',
    [['campaigns', campaignId]],
  );

  const cancelMutation = useApiMutation<unknown, void>(
    `/campaigns/${campaignId}/cancel`,
    'post',
    [['campaigns', campaignId]],
  );

  // Subscribe to campaign updates
  useEffect(() => {
    emit('subscribe:campaign', { campaignId });

    const unsubProgress = on('campaign:progress', (data: unknown) => {
      const progress = data as CampaignMetrics & { campaignId: string };
      if (progress.campaignId === campaignId) {
        setMetrics(progress);
      }
    });

    const unsubCallStarted = on('call:started', (data: unknown) => {
      const call = data as ActiveCall & { campaignId?: string };
      if (call.campaignId === campaignId) {
        setActiveCalls((prev) => [...prev, call]);
      }
    });

    const unsubCallEnded = on('call:ended', (data: unknown) => {
      const call = data as { callId: string };
      setActiveCalls((prev) => prev.filter((c) => c.callId !== call.callId));
    });

    return () => {
      emit('unsubscribe:campaign', { campaignId });
      if (unsubProgress) unsubProgress();
      if (unsubCallStarted) unsubCallStarted();
      if (unsubCallEnded) unsubCallEnded();
    };
  }, [campaignId, emit, on]);

  // Update elapsed timer
  useEffect(() => {
    if (!metrics?.startedAt || status !== 'RUNNING') return;

    const interval = setInterval(() => {
      setElapsed(formatElapsed(metrics.startedAt));
    }, 10000);

    setElapsed(formatElapsed(metrics.startedAt));
    return () => clearInterval(interval);
  }, [metrics?.startedAt, status]);

  const handleStart = useCallback(async () => {
    await startMutation.mutateAsync(undefined as never);
    setStatus('RUNNING');
  }, [startMutation]);

  const handlePause = useCallback(async () => {
    await pauseMutation.mutateAsync(undefined as never);
    setStatus('PAUSED');
  }, [pauseMutation]);

  const handleResume = useCallback(async () => {
    await resumeMutation.mutateAsync(undefined as never);
    setStatus('RUNNING');
  }, [resumeMutation]);

  const handleCancel = useCallback(async () => {
    if (window.confirm('Are you sure you want to cancel this campaign? This cannot be undone.')) {
      await cancelMutation.mutateAsync(undefined as never);
      setStatus('CANCELLED');
    }
  }, [cancelMutation]);

  const progressPercent = metrics
    ? Math.round((metrics.contactsProcessed / Math.max(metrics.totalContacts, 1)) * 100)
    : 0;

  const isRunning = status === 'RUNNING';
  const isPaused = status === 'PAUSED';
  const isActive = isRunning || isPaused;

  return (
    <div className="space-y-4">
      {/* Header with controls */}
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-lg">{campaignName}</CardTitle>
              <div className="mt-1 flex items-center gap-2">
                <Badge variant={isRunning ? 'default' : isPaused ? 'secondary' : 'outline'}>
                  {status}
                </Badge>
                {metrics?.startedAt && (
                  <span className="text-xs text-muted-foreground">
                    Running for {elapsed}
                  </span>
                )}
              </div>
            </div>

            <div className="flex items-center gap-2">
              {status === 'DRAFT' || status === 'SCHEDULED' ? (
                <Button onClick={handleStart} size="sm" className="gap-1.5">
                  <Play className="h-4 w-4" />
                  Start
                </Button>
              ) : isRunning ? (
                <Button onClick={handlePause} size="sm" variant="secondary" className="gap-1.5">
                  <Pause className="h-4 w-4" />
                  Pause
                </Button>
              ) : isPaused ? (
                <Button onClick={handleResume} size="sm" className="gap-1.5">
                  <RefreshCw className="h-4 w-4" />
                  Resume
                </Button>
              ) : null}

              {isActive && (
                <Button onClick={handleCancel} size="sm" variant="destructive" className="gap-1.5">
                  <Square className="h-4 w-4" />
                  Cancel
                </Button>
              )}
            </div>
          </div>
        </CardHeader>

        {/* Progress bar */}
        {metrics && (
          <CardContent className="pt-0">
            <div className="space-y-2">
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>{metrics.contactsProcessed.toLocaleString()} / {metrics.totalContacts.toLocaleString()} contacts</span>
                <span>{progressPercent}%</span>
              </div>
              <div className="h-2 w-full rounded-full bg-muted">
                <div
                  className={cn(
                    'h-full rounded-full transition-all duration-500',
                    isRunning ? 'bg-primary' : 'bg-primary/60',
                  )}
                  style={{ width: `${progressPercent}%` }}
                />
              </div>
            </div>
          </CardContent>
        )}
      </Card>

      {/* Live metrics grid */}
      {metrics && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-6">
          <MetricCard
            icon={Users}
            label="Active Calls"
            value={metrics.activeCalls.toString()}
            color="text-blue-600 dark:text-blue-400"
            pulse={metrics.activeCalls > 0}
          />
          <MetricCard
            icon={CheckCircle}
            label="Connected"
            value={metrics.connectedCalls.toLocaleString()}
            color="text-green-600 dark:text-green-400"
          />
          <MetricCard
            icon={XCircle}
            label="Failed"
            value={metrics.failedCalls.toLocaleString()}
            color="text-red-600 dark:text-red-400"
          />
          <MetricCard
            icon={Activity}
            label="Calls/min"
            value={metrics.callsPerMinute.toFixed(1)}
            color="text-purple-600 dark:text-purple-400"
          />
          <MetricCard
            icon={TrendingUp}
            label="Success Rate"
            value={`${metrics.successRate.toFixed(1)}%`}
            color="text-emerald-600 dark:text-emerald-400"
          />
          <MetricCard
            icon={Clock}
            label="Avg Duration"
            value={formatDuration(Math.round(metrics.averageDurationSeconds))}
            color="text-orange-600 dark:text-orange-400"
          />
        </div>
      )}

      {/* Detailed stats */}
      {metrics && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center gap-2">
              <BarChart3 className="h-4 w-4" />
              Detailed Breakdown
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <StatItem label="Remaining" value={metrics.contactsRemaining.toLocaleString()} />
              <StatItem label="Busy" value={metrics.busyCalls.toLocaleString()} />
              <StatItem label="No Answer" value={metrics.noAnswerCalls.toLocaleString()} />
              <StatItem label="Retries" value={metrics.totalRetries.toLocaleString()} />
            </div>
          </CardContent>
        </Card>
      )}

      {/* Active calls list */}
      {activeCalls.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center gap-2">
              <PhoneCall className="h-4 w-4" />
              Active Calls ({activeCalls.length})
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              {activeCalls.map((call) => (
                <div
                  key={call.callId}
                  className="flex items-center justify-between rounded-md border p-2.5"
                >
                  <div className="flex items-center gap-3">
                    <div className="h-2 w-2 rounded-full bg-green-500 animate-pulse" />
                    <div>
                      <p className="text-xs font-medium">
                        {call.contactName || call.contactPhone || call.callId.slice(0, 8)}
                      </p>
                      <p className="text-[10px] text-muted-foreground">{call.phase}</p>
                    </div>
                  </div>
                  <span className="text-xs font-mono text-muted-foreground">
                    {formatDuration(call.duration)}
                  </span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

// ─── SUB-COMPONENTS ──────────────────────────────────────────────────────────

function MetricCard({
  icon: Icon,
  label,
  value,
  color,
  pulse,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
  color: string;
  pulse?: boolean;
}) {
  return (
    <Card>
      <CardContent className="p-3">
        <div className="flex items-center gap-2.5">
          <div className="relative">
            <Icon className={cn('h-4 w-4', color)} />
            {pulse && (
              <span className="absolute -top-0.5 -right-0.5 h-2 w-2 rounded-full bg-green-500 animate-pulse" />
            )}
          </div>
          <div>
            <p className="text-[10px] text-muted-foreground">{label}</p>
            <p className="text-sm font-bold">{value}</p>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function StatItem({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="text-sm font-semibold">{value}</p>
    </div>
  );
}

function cn(...classes: (string | boolean | undefined)[]): string {
  return classes.filter(Boolean).join(' ');
}
