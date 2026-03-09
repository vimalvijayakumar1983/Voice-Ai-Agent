'use client';

import Link from 'next/link';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Plus, Phone, PhoneCall, TrendingUp, MoreVertical } from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

const mockCampaigns = [
  { id: '1', name: 'Q1 Lead Generation', status: 'running', agent: 'Sales Bot A', totalCalls: 5000, completed: 3400, connected: 2100, conversion: 24, startDate: 'Feb 1', endDate: 'Mar 31' },
  { id: '2', name: 'Customer Re-engagement', status: 'running', agent: 'Sales Bot B', totalCalls: 2000, completed: 840, connected: 520, conversion: 18, startDate: 'Feb 15', endDate: 'Mar 15' },
  { id: '3', name: 'Appointment Reminders', status: 'scheduled', agent: 'Booking Bot', totalCalls: 800, completed: 0, connected: 0, conversion: 0, startDate: 'Mar 10', endDate: 'Mar 10' },
  { id: '4', name: 'Product Launch Outreach', status: 'draft', agent: 'Sales Bot A', totalCalls: 10000, completed: 0, connected: 0, conversion: 0, startDate: 'Apr 1', endDate: 'Apr 30' },
  { id: '5', name: 'Holiday Promo', status: 'completed', agent: 'Sales Bot B', totalCalls: 3000, completed: 3000, connected: 1950, conversion: 22, startDate: 'Dec 1', endDate: 'Dec 25' },
  { id: '6', name: 'Collections Q4', status: 'paused', agent: 'Collections Bot', totalCalls: 1500, completed: 600, connected: 420, conversion: 35, startDate: 'Jan 1', endDate: 'Jan 31' },
];

const statusConfig: Record<string, { variant: 'success' | 'warning' | 'secondary' | 'default' | 'destructive'; dot?: string }> = {
  running: { variant: 'success', dot: 'bg-green-500 animate-pulse' },
  scheduled: { variant: 'warning' },
  draft: { variant: 'secondary' },
  completed: { variant: 'default' },
  paused: { variant: 'warning' },
};

export default function CampaignsPage() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">Campaigns</h2>
          <p className="text-sm text-muted-foreground">
            Manage outbound calling campaigns
          </p>
        </div>
        <Button asChild>
          <Link href="/campaigns/new">
            <Plus className="mr-2 h-4 w-4" />
            Create Campaign
          </Link>
        </Button>
      </div>

      <div className="grid gap-4">
        {mockCampaigns.map((campaign) => (
          <Link key={campaign.id} href={`/campaigns/${campaign.id}`}>
            <Card className="transition-all hover:shadow-md hover:border-primary/20">
              <CardContent className="p-5">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-4">
                    <div>
                      <div className="flex items-center gap-3">
                        <h3 className="font-semibold">{campaign.name}</h3>
                        <Badge variant={statusConfig[campaign.status]?.variant || 'secondary'}>
                          {statusConfig[campaign.status]?.dot && (
                            <span className={`mr-1.5 h-1.5 w-1.5 rounded-full ${statusConfig[campaign.status].dot}`} />
                          )}
                          {campaign.status}
                        </Badge>
                      </div>
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        {campaign.agent} &middot; {campaign.startDate} - {campaign.endDate}
                      </p>
                    </div>
                  </div>

                  <div className="flex items-center gap-8">
                    <div className="flex items-center gap-6 text-sm">
                      <div className="flex items-center gap-1.5">
                        <Phone className="h-3.5 w-3.5 text-muted-foreground" />
                        <span className="text-muted-foreground">{campaign.completed.toLocaleString()}/{campaign.totalCalls.toLocaleString()}</span>
                      </div>
                      <div className="flex items-center gap-1.5">
                        <PhoneCall className="h-3.5 w-3.5 text-muted-foreground" />
                        <span className="text-muted-foreground">{campaign.connected.toLocaleString()} connected</span>
                      </div>
                      <div className="flex items-center gap-1.5">
                        <TrendingUp className="h-3.5 w-3.5 text-muted-foreground" />
                        <span className="text-muted-foreground">{campaign.conversion}% conv.</span>
                      </div>
                    </div>

                    {/* Progress bar */}
                    <div className="w-32">
                      <div className="h-2 rounded-full bg-secondary">
                        <div
                          className="h-full rounded-full bg-primary transition-all"
                          style={{ width: `${(campaign.completed / campaign.totalCalls) * 100}%` }}
                        />
                      </div>
                      <p className="mt-1 text-right text-[10px] text-muted-foreground">
                        {Math.round((campaign.completed / campaign.totalCalls) * 100)}%
                      </p>
                    </div>

                    <DropdownMenu>
                      <DropdownMenuTrigger asChild onClick={(e) => e.preventDefault()}>
                        <Button variant="ghost" size="icon" className="h-8 w-8">
                          <MoreVertical className="h-4 w-4" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem>Pause</DropdownMenuItem>
                        <DropdownMenuItem>Duplicate</DropdownMenuItem>
                        <DropdownMenuItem>Export Data</DropdownMenuItem>
                        <DropdownMenuItem className="text-destructive">Cancel</DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                </div>
              </CardContent>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}
