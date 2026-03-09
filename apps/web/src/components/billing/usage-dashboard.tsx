'use client';

import { useMemo } from 'react';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import {
  Phone,
  Zap,
  Mic,
  MessageSquare,
  Brain,
  HardDrive,
  TrendingUp,
  Calendar,
  AlertTriangle,
} from 'lucide-react';
import { cn, formatNumber, formatCurrency } from '@/lib/utils';

// ---------- Types ----------

interface UsageSummary {
  dimension: string;
  used: number;
  limit: number;
  percentage: number;
  unit: string;
}

interface UsageHistory {
  date: string;
  quantity: number;
}

interface CostBreakdown {
  dimension: string;
  cost: number;
  quantity: number;
}

interface UsageDashboardProps {
  usage: UsageSummary[];
  history?: Record<string, UsageHistory[]>;
  costs?: CostBreakdown[];
  currentPlan?: string;
  billingPeriod?: { start: string; end: string };
  className?: string;
}

const DIMENSION_CONFIG: Record<
  string,
  {
    label: string;
    icon: React.ElementType;
    color: string;
    bgColor: string;
    formatValue: (value: number) => string;
  }
> = {
  call_minutes: {
    label: 'Call Minutes',
    icon: Phone,
    color: 'text-blue-600 dark:text-blue-400',
    bgColor: 'bg-blue-500',
    formatValue: (v) => `${formatNumber(v)} min`,
  },
  api_calls: {
    label: 'API Calls',
    icon: Zap,
    color: 'text-purple-600 dark:text-purple-400',
    bgColor: 'bg-purple-500',
    formatValue: (v) => formatNumber(v),
  },
  transcription_minutes: {
    label: 'Transcription',
    icon: Mic,
    color: 'text-green-600 dark:text-green-400',
    bgColor: 'bg-green-500',
    formatValue: (v) => `${formatNumber(v)} min`,
  },
  tts_characters: {
    label: 'TTS Characters',
    icon: MessageSquare,
    color: 'text-orange-600 dark:text-orange-400',
    bgColor: 'bg-orange-500',
    formatValue: (v) => formatNumber(v),
  },
  llm_tokens: {
    label: 'LLM Tokens',
    icon: Brain,
    color: 'text-pink-600 dark:text-pink-400',
    bgColor: 'bg-pink-500',
    formatValue: (v) => formatNumber(v),
  },
  storage_gb: {
    label: 'Storage',
    icon: HardDrive,
    color: 'text-slate-600 dark:text-slate-400',
    bgColor: 'bg-slate-500',
    formatValue: (v) => `${v.toFixed(2)} GB`,
  },
};

// ---------- Component ----------

export function UsageDashboard({
  usage,
  history,
  costs,
  currentPlan,
  billingPeriod,
  className,
}: UsageDashboardProps) {
  const totalCost = useMemo(
    () => costs?.reduce((sum, c) => sum + c.cost, 0) || 0,
    [costs],
  );

  const warnings = useMemo(
    () => usage.filter((u) => u.percentage >= 80 && u.limit > 0),
    [usage],
  );

  return (
    <div className={cn('space-y-6', className)}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">Usage Overview</h2>
          {billingPeriod && (
            <p className="text-sm text-muted-foreground flex items-center gap-1">
              <Calendar className="h-3.5 w-3.5" />
              {new Date(billingPeriod.start).toLocaleDateString()} -{' '}
              {new Date(billingPeriod.end).toLocaleDateString()}
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          {currentPlan && (
            <Badge variant="outline" className="capitalize">
              {currentPlan} Plan
            </Badge>
          )}
          {totalCost > 0 && (
            <Badge variant="secondary">
              Est. {formatCurrency(totalCost)}
            </Badge>
          )}
        </div>
      </div>

      {/* Warnings */}
      {warnings.length > 0 && (
        <div className="rounded-lg border border-yellow-200 bg-yellow-50 dark:border-yellow-900/50 dark:bg-yellow-900/10 p-4">
          <div className="flex items-center gap-2 mb-2">
            <AlertTriangle className="h-4 w-4 text-yellow-600 dark:text-yellow-400" />
            <span className="text-sm font-medium text-yellow-800 dark:text-yellow-200">
              Usage Alerts
            </span>
          </div>
          <ul className="space-y-1">
            {warnings.map((w) => {
              const config = DIMENSION_CONFIG[w.dimension];
              return (
                <li key={w.dimension} className="text-xs text-yellow-700 dark:text-yellow-300">
                  {config?.label || w.dimension}: {Math.round(w.percentage)}% used
                  ({config?.formatValue(w.used)} of{' '}
                  {w.limit === -1 ? 'unlimited' : config?.formatValue(w.limit)})
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {/* Usage Meters */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {usage.map((item) => {
          const config = DIMENSION_CONFIG[item.dimension] || {
            label: item.dimension,
            icon: Zap,
            color: 'text-gray-600',
            bgColor: 'bg-gray-500',
            formatValue: (v: number) => `${v} ${item.unit}`,
          };

          const Icon = config.icon;
          const isUnlimited = item.limit === -1;
          const percentage = isUnlimited ? 0 : Math.min(item.percentage, 100);

          return (
            <Card key={item.dimension}>
              <CardContent className="p-4 space-y-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Icon className={cn('h-4 w-4', config.color)} />
                    <span className="text-sm font-medium">{config.label}</span>
                  </div>
                  {!isUnlimited && item.percentage >= 90 && (
                    <AlertTriangle className="h-4 w-4 text-yellow-500" />
                  )}
                </div>

                <div className="space-y-1">
                  <div className="flex items-baseline justify-between">
                    <span className="text-2xl font-bold">
                      {config.formatValue(item.used)}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {isUnlimited
                        ? 'Unlimited'
                        : `of ${config.formatValue(item.limit)}`}
                    </span>
                  </div>

                  {/* Progress Bar */}
                  {!isUnlimited && (
                    <div className="h-2 w-full bg-muted rounded-full overflow-hidden">
                      <div
                        className={cn(
                          'h-full rounded-full transition-all duration-500',
                          percentage >= 100
                            ? 'bg-red-500'
                            : percentage >= 90
                              ? 'bg-yellow-500'
                              : percentage >= 80
                                ? 'bg-yellow-400'
                                : config.bgColor,
                        )}
                        style={{ width: `${percentage}%` }}
                      />
                    </div>
                  )}

                  {!isUnlimited && (
                    <p
                      className={cn(
                        'text-xs',
                        percentage >= 90
                          ? 'text-yellow-600 dark:text-yellow-400 font-medium'
                          : 'text-muted-foreground',
                      )}
                    >
                      {Math.round(percentage)}% used
                    </p>
                  )}
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      {/* Cost Breakdown */}
      {costs && costs.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Cost Breakdown</CardTitle>
            <CardDescription>Estimated costs for current billing period</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              {costs.map((cost) => {
                const config = DIMENSION_CONFIG[cost.dimension];
                return (
                  <div
                    key={cost.dimension}
                    className="flex items-center justify-between py-2 border-b last:border-0"
                  >
                    <div className="flex items-center gap-2">
                      {config && (
                        <config.icon
                          className={cn('h-4 w-4', config.color)}
                        />
                      )}
                      <span className="text-sm">
                        {config?.label || cost.dimension}
                      </span>
                      <span className="text-xs text-muted-foreground">
                        ({config?.formatValue(cost.quantity) || cost.quantity})
                      </span>
                    </div>
                    <span className="text-sm font-medium">
                      {formatCurrency(cost.cost)}
                    </span>
                  </div>
                );
              })}

              <div className="flex items-center justify-between pt-2 border-t font-medium">
                <span className="text-sm">Total Estimated</span>
                <span className="text-base">{formatCurrency(totalCost)}</span>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Historical Chart (simplified bar chart) */}
      {history && Object.keys(history).length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <TrendingUp className="h-4 w-4" />
              Usage Trends
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              {Object.entries(history)
                .slice(0, 3)
                .map(([dimension, entries]) => {
                  const config = DIMENSION_CONFIG[dimension];
                  const maxValue = Math.max(...entries.map((e) => e.quantity), 1);

                  return (
                    <div key={dimension} className="space-y-2">
                      <div className="flex items-center gap-2">
                        {config && (
                          <config.icon className={cn('h-3.5 w-3.5', config.color)} />
                        )}
                        <span className="text-xs font-medium">
                          {config?.label || dimension}
                        </span>
                      </div>
                      <div className="flex items-end gap-1 h-16">
                        {entries.slice(-14).map((entry) => {
                          const height = (entry.quantity / maxValue) * 100;
                          return (
                            <div
                              key={entry.date}
                              className="flex-1 flex flex-col items-center gap-0.5"
                              title={`${entry.date}: ${config?.formatValue(entry.quantity)}`}
                            >
                              <div
                                className={cn(
                                  'w-full rounded-t transition-all',
                                  config?.bgColor || 'bg-primary',
                                  'opacity-70 hover:opacity-100',
                                )}
                                style={{
                                  height: `${Math.max(height, 2)}%`,
                                  minHeight: '2px',
                                }}
                              />
                            </div>
                          );
                        })}
                      </div>
                      <div className="flex justify-between text-[10px] text-muted-foreground">
                        <span>
                          {entries.length > 0 ? entries[Math.max(0, entries.length - 14)]?.date : ''}
                        </span>
                        <span>
                          {entries.length > 0 ? entries[entries.length - 1]?.date : ''}
                        </span>
                      </div>
                    </div>
                  );
                })}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
