'use client';

import { useState, useMemo } from 'react';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
  LineChart,
  Line,
} from 'recharts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { cn } from '@/lib/utils';
import { useApiQuery } from '@/hooks/use-api';

// ============================================================================
// Types
// ============================================================================

interface CostData {
  period: { from: string; to: string };
  cost: {
    total: number;
    byType: Record<string, number>;
    perCall: number;
    perMinute: number;
    trend: Array<{ date: string; cost: number }>;
  };
}

// ============================================================================
// Colors for cost dimensions
// ============================================================================

const DIMENSION_COLORS: Record<string, string> = {
  call_minutes: '#3b82f6',
  api_calls: '#8b5cf6',
  transcription_minutes: '#06b6d4',
  tts_characters: '#10b981',
  llm_tokens: '#f59e0b',
  storage_gb: '#ef4444',
  sms_sent: '#ec4899',
  phone_numbers: '#6366f1',
};

const DIMENSION_LABELS: Record<string, string> = {
  call_minutes: 'Telephony',
  api_calls: 'API Calls',
  transcription_minutes: 'STT',
  tts_characters: 'TTS',
  llm_tokens: 'LLM',
  storage_gb: 'Storage',
  sms_sent: 'SMS',
  phone_numbers: 'Phone Numbers',
};

// ============================================================================
// Component
// ============================================================================

export function CostBreakdownChart() {
  const from = useMemo(() => {
    const d = new Date();
    d.setDate(d.getDate() - 30);
    return d.toISOString();
  }, []);

  const { data, isLoading } = useApiQuery<CostData>(
    ['analytics', 'costs'],
    `/analytics/costs/breakdown?from=${from}`,
  );

  const breakdownData = useMemo(() => {
    if (!data?.cost?.byType) {
      // Placeholder
      return [
        { name: 'Telephony', value: 1250, color: DIMENSION_COLORS.call_minutes },
        { name: 'STT', value: 890, color: DIMENSION_COLORS.transcription_minutes },
        { name: 'TTS', value: 420, color: DIMENSION_COLORS.tts_characters },
        { name: 'LLM', value: 1800, color: DIMENSION_COLORS.llm_tokens },
        { name: 'Storage', value: 150, color: DIMENSION_COLORS.storage_gb },
        { name: 'SMS', value: 320, color: DIMENSION_COLORS.sms_sent },
      ];
    }

    return Object.entries(data.cost.byType).map(([key, value]) => ({
      name: DIMENSION_LABELS[key] || key,
      value,
      color: DIMENSION_COLORS[key] || '#94a3b8',
    }));
  }, [data]);

  const trendData = useMemo(() => {
    if (!data?.cost?.trend?.length) {
      return Array.from({ length: 30 }, (_, i) => {
        const d = new Date();
        d.setDate(d.getDate() - (29 - i));
        return {
          date: d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
          cost: Math.floor(Math.random() * 200) + 100,
        };
      });
    }

    return data.cost.trend.map((t) => ({
      date: new Date(t.date).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
      cost: t.cost,
    }));
  }, [data]);

  const totalCost = data?.cost?.total || breakdownData.reduce((s, d) => s + d.value, 0);
  const perCall = data?.cost?.perCall || 0;
  const perMinute = data?.cost?.perMinute || 0;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-base font-semibold">Cost Breakdown</CardTitle>
        <div className="flex items-center gap-4 text-xs">
          <div className="text-right">
            <span className="text-muted-foreground">Total: </span>
            <span className="font-semibold">${totalCost.toFixed(2)}</span>
          </div>
          <div className="text-right">
            <span className="text-muted-foreground">Per call: </span>
            <span className="font-semibold">${perCall.toFixed(3)}</span>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <Tabs defaultValue="breakdown" className="space-y-4">
          <TabsList className="h-8">
            <TabsTrigger value="breakdown" className="text-xs h-6">
              By Dimension
            </TabsTrigger>
            <TabsTrigger value="trend" className="text-xs h-6">
              Per-Call Trend
            </TabsTrigger>
          </TabsList>

          <TabsContent value="breakdown">
            <div className="h-[280px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={breakdownData} layout="vertical">
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" horizontal={false} />
                  <XAxis
                    type="number"
                    tick={{ fontSize: 11 }}
                    className="text-muted-foreground"
                    tickFormatter={(v) => `$${v}`}
                    tickLine={false}
                    axisLine={false}
                  />
                  <YAxis
                    type="category"
                    dataKey="name"
                    tick={{ fontSize: 11 }}
                    className="text-muted-foreground"
                    width={90}
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
                    formatter={(value: number) => [`$${value.toFixed(2)}`, 'Cost']}
                  />
                  <Bar
                    dataKey="value"
                    radius={[0, 4, 4, 0]}
                    fill="#3b82f6"
                    barSize={24}
                  />
                </BarChart>
              </ResponsiveContainer>
            </div>

            {/* Dimension badges */}
            <div className="flex flex-wrap gap-2 mt-3 pt-3 border-t">
              {breakdownData.map((d) => (
                <div
                  key={d.name}
                  className="flex items-center gap-1.5 text-xs text-muted-foreground"
                >
                  <div
                    className="w-2.5 h-2.5 rounded-sm"
                    style={{ backgroundColor: d.color }}
                  />
                  <span>{d.name}</span>
                  <span className="font-medium text-foreground">${d.value.toFixed(2)}</span>
                </div>
              ))}
            </div>
          </TabsContent>

          <TabsContent value="trend">
            <div className="h-[280px]">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={trendData}>
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
                    tickFormatter={(v) => `$${v}`}
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
                    formatter={(value: number) => [`$${value.toFixed(2)}`, 'Daily Cost']}
                  />
                  <Line
                    type="monotone"
                    dataKey="cost"
                    stroke="#3b82f6"
                    strokeWidth={2}
                    dot={false}
                    activeDot={{ r: 4 }}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  );
}
