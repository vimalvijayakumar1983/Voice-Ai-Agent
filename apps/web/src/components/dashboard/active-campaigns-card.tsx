'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import Link from 'next/link';

// Simple progress component inline since it's lightweight
function ProgressBar({ value, className }: { value: number; className?: string }) {
  return (
    <div className={`relative h-2 w-full overflow-hidden rounded-full bg-secondary ${className || ''}`}>
      <div
        className="h-full rounded-full bg-primary transition-all"
        style={{ width: `${Math.min(100, Math.max(0, value))}%` }}
      />
    </div>
  );
}

const mockCampaigns = [
  {
    id: '1',
    name: 'Q1 Lead Generation',
    status: 'running',
    progress: 68,
    totalCalls: 5000,
    completedCalls: 3400,
    connected: 2100,
  },
  {
    id: '2',
    name: 'Customer Re-engagement',
    status: 'running',
    progress: 42,
    totalCalls: 2000,
    completedCalls: 840,
    connected: 520,
  },
  {
    id: '3',
    name: 'Appointment Reminders',
    status: 'scheduled',
    progress: 0,
    totalCalls: 800,
    completedCalls: 0,
    connected: 0,
  },
];

const statusBadgeVariant: Record<string, 'success' | 'warning' | 'secondary' | 'default'> = {
  running: 'success',
  scheduled: 'warning',
  paused: 'secondary',
  draft: 'secondary',
};

export function ActiveCampaignsCard() {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-base font-semibold">Active Campaigns</CardTitle>
        <Link href="/campaigns" className="text-sm text-primary hover:underline">
          View all
        </Link>
      </CardHeader>
      <CardContent className="space-y-5">
        {mockCampaigns.map((campaign) => (
          <div key={campaign.id} className="space-y-2">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">{campaign.name}</span>
                <Badge variant={statusBadgeVariant[campaign.status] || 'secondary'} className="text-[10px]">
                  {campaign.status}
                </Badge>
              </div>
              <span className="text-xs text-muted-foreground">
                {campaign.completedCalls.toLocaleString()} / {campaign.totalCalls.toLocaleString()}
              </span>
            </div>
            <ProgressBar value={campaign.progress} />
            <div className="flex justify-between text-xs text-muted-foreground">
              <span>{campaign.progress}% complete</span>
              <span>{campaign.connected.toLocaleString()} connected</span>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
