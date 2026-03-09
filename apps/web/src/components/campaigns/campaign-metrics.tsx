'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Phone, PhoneCall, CheckCircle, XCircle, Clock, TrendingUp } from 'lucide-react';

interface CampaignMetricsProps {
  totalCalls: number;
  connectedCalls: number;
  completedCalls: number;
  failedCalls: number;
  avgDuration: string;
  conversionRate: number;
}

export function CampaignMetrics({
  totalCalls,
  connectedCalls,
  completedCalls,
  failedCalls,
  avgDuration,
  conversionRate,
}: CampaignMetricsProps) {
  const metrics = [
    { label: 'Total Calls', value: totalCalls.toLocaleString(), icon: Phone, color: 'text-blue-600 dark:text-blue-400' },
    { label: 'Connected', value: connectedCalls.toLocaleString(), icon: PhoneCall, color: 'text-green-600 dark:text-green-400' },
    { label: 'Completed', value: completedCalls.toLocaleString(), icon: CheckCircle, color: 'text-emerald-600 dark:text-emerald-400' },
    { label: 'Failed', value: failedCalls.toLocaleString(), icon: XCircle, color: 'text-red-600 dark:text-red-400' },
    { label: 'Avg Duration', value: avgDuration, icon: Clock, color: 'text-orange-600 dark:text-orange-400' },
    { label: 'Conversion', value: `${conversionRate}%`, icon: TrendingUp, color: 'text-violet-600 dark:text-violet-400' },
  ];

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
      {metrics.map((metric) => (
        <Card key={metric.label}>
          <CardContent className="p-4">
            <div className="flex items-center gap-3">
              <metric.icon className={`h-5 w-5 ${metric.color}`} />
              <div>
                <p className="text-xs text-muted-foreground">{metric.label}</p>
                <p className="text-lg font-bold">{metric.value}</p>
              </div>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
