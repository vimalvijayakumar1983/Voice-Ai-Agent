'use client';

import { useMemo } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { cn } from '@/lib/utils';
import { useApiQuery } from '@/hooks/use-api';
import { useState } from 'react';

// ============================================================================
// Types
// ============================================================================

interface FunnelData {
  campaignId: string;
  campaignName: string;
  totalContacts: number;
  called: number;
  answered: number;
  completed: number;
  converted: number;
  conversionRates: {
    calledRate: number;
    answerRate: number;
    completionRate: number;
    conversionRate: number;
  };
}

interface FunnelResponse {
  funnels: FunnelData[];
}

// ============================================================================
// Funnel Stage
// ============================================================================

interface FunnelStageProps {
  label: string;
  value: number;
  maxValue: number;
  rate?: number;
  color: string;
  index: number;
}

function FunnelStage({ label, value, maxValue, rate, color, index }: FunnelStageProps) {
  const widthPercent = maxValue > 0 ? (value / maxValue) * 100 : 0;
  const minWidth = 30; // minimum visual width
  const displayWidth = Math.max(minWidth, widthPercent);

  return (
    <div className="flex items-center gap-4">
      <div className="w-32 text-right">
        <p className="text-sm font-medium">{label}</p>
        {rate !== undefined && (
          <p className="text-[10px] text-muted-foreground">{rate.toFixed(1)}% conversion</p>
        )}
      </div>
      <div className="flex-1 relative">
        <div
          className={cn('h-12 rounded-md flex items-center justify-center transition-all duration-500', color)}
          style={{
            width: `${displayWidth}%`,
            marginLeft: `${(100 - displayWidth) / 2}%`,
          }}
        >
          <span className="text-sm font-bold text-white">{value.toLocaleString()}</span>
        </div>
      </div>
      {index > 0 && rate !== undefined && (
        <div className="w-16 text-left">
          <span
            className={cn(
              'text-xs font-semibold',
              rate >= 50 ? 'text-green-600' : rate >= 25 ? 'text-yellow-600' : 'text-red-600',
            )}
          >
            {rate.toFixed(0)}%
          </span>
        </div>
      )}
      {index === 0 && <div className="w-16" />}
    </div>
  );
}

// ============================================================================
// Component
// ============================================================================

export function CampaignFunnel() {
  const [selectedCampaign, setSelectedCampaign] = useState<string>('all');

  const { data, isLoading } = useApiQuery<FunnelResponse>(
    ['analytics', 'campaign-funnel', selectedCampaign],
    selectedCampaign === 'all'
      ? '/analytics/campaigns/funnel'
      : `/analytics/campaigns/funnel?campaignId=${selectedCampaign}`,
  );

  const funnel = useMemo(() => {
    if (!data?.funnels?.length) {
      // Placeholder data
      return {
        campaignName: 'All Campaigns',
        totalContacts: 5000,
        called: 3200,
        answered: 2100,
        completed: 1400,
        converted: 480,
        conversionRates: {
          calledRate: 64,
          answerRate: 65.6,
          completionRate: 66.7,
          conversionRate: 34.3,
        },
      } as FunnelData;
    }

    // Aggregate all funnels if 'all' is selected
    if (selectedCampaign === 'all' && data.funnels.length > 1) {
      const agg = data.funnels.reduce(
        (acc, f) => ({
          totalContacts: acc.totalContacts + f.totalContacts,
          called: acc.called + f.called,
          answered: acc.answered + f.answered,
          completed: acc.completed + f.completed,
          converted: acc.converted + f.converted,
        }),
        { totalContacts: 0, called: 0, answered: 0, completed: 0, converted: 0 },
      );

      return {
        campaignName: 'All Campaigns',
        ...agg,
        conversionRates: {
          calledRate: agg.totalContacts > 0 ? (agg.called / agg.totalContacts) * 100 : 0,
          answerRate: agg.called > 0 ? (agg.answered / agg.called) * 100 : 0,
          completionRate: agg.answered > 0 ? (agg.completed / agg.answered) * 100 : 0,
          conversionRate: agg.completed > 0 ? (agg.converted / agg.completed) * 100 : 0,
        },
      } as FunnelData;
    }

    return data.funnels[0];
  }, [data, selectedCampaign]);

  const stages = useMemo(
    () => [
      {
        label: 'Total Contacts',
        value: funnel.totalContacts,
        color: 'bg-blue-500',
      },
      {
        label: 'Called',
        value: funnel.called,
        rate: funnel.conversionRates.calledRate,
        color: 'bg-indigo-500',
      },
      {
        label: 'Answered',
        value: funnel.answered,
        rate: funnel.conversionRates.answerRate,
        color: 'bg-violet-500',
      },
      {
        label: 'Completed',
        value: funnel.completed,
        rate: funnel.conversionRates.completionRate,
        color: 'bg-purple-500',
      },
      {
        label: 'Converted',
        value: funnel.converted,
        rate: funnel.conversionRates.conversionRate,
        color: 'bg-fuchsia-500',
      },
    ],
    [funnel],
  );

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-base font-semibold">Campaign Funnel</CardTitle>
        <Select value={selectedCampaign} onValueChange={setSelectedCampaign}>
          <SelectTrigger className="w-[200px] h-8 text-xs">
            <SelectValue placeholder="Select campaign" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Campaigns</SelectItem>
            {data?.funnels?.map((f) => (
              <SelectItem key={f.campaignId} value={f.campaignId}>
                {f.campaignName}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </CardHeader>
      <CardContent className="pt-4">
        <div className="space-y-3">
          {stages.map((stage, i) => (
            <FunnelStage
              key={stage.label}
              label={stage.label}
              value={stage.value}
              maxValue={stages[0].value}
              rate={stage.rate}
              color={stage.color}
              index={i}
            />
          ))}
        </div>

        {/* Overall conversion */}
        <div className="mt-6 pt-4 border-t text-center">
          <p className="text-sm text-muted-foreground">Overall Conversion Rate</p>
          <p className="text-3xl font-bold text-primary mt-1">
            {funnel.totalContacts > 0
              ? ((funnel.converted / funnel.totalContacts) * 100).toFixed(1)
              : '0.0'}
            %
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
