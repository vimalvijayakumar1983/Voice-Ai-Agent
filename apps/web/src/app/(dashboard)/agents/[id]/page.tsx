'use client';

import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { KpiCard } from '@/components/dashboard/kpi-card';
import {
  Phone,
  PhoneCall,
  Clock,
  TrendingUp,
  Settings,
  Play,
  Pause,
  Copy,
  MoreVertical,
} from 'lucide-react';

export default function AgentDetailPage({ params }: { params: { id: string } }) {
  return (
    <div className="space-y-6">
      {/* Agent header */}
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-4">
          <div className="flex h-14 w-14 items-center justify-center rounded-xl bg-primary/10 text-2xl">
            📞
          </div>
          <div>
            <div className="flex items-center gap-3">
              <h2 className="text-2xl font-bold tracking-tight">Sales Outreach Pro</h2>
              <Badge variant="success">Active</Badge>
            </div>
            <p className="text-sm text-muted-foreground">
              Outbound Sales &middot; Technology &middot; English (US)
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm">
            <Pause className="mr-2 h-4 w-4" />
            Pause
          </Button>
          <Button variant="outline" size="sm">
            <Copy className="mr-2 h-4 w-4" />
            Duplicate
          </Button>
          <Button size="sm">
            <Settings className="mr-2 h-4 w-4" />
            Edit
          </Button>
        </div>
      </div>

      {/* KPI row */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <KpiCard title="Total Calls" value="12,450" change={15.2} icon={Phone} />
        <KpiCard title="Connected" value="8,920" change={12.1} icon={PhoneCall} />
        <KpiCard title="Avg Duration" value="4m 12s" change={-1.3} icon={Clock} />
        <KpiCard title="Success Rate" value="68%" change={4.5} icon={TrendingUp} />
      </div>

      {/* Tabs */}
      <Tabs defaultValue="overview" className="space-y-4">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="configuration">Configuration</TabsTrigger>
          <TabsTrigger value="calls">Call History</TabsTrigger>
          <TabsTrigger value="performance">Performance</TabsTrigger>
          <TabsTrigger value="versions">Versions</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="space-y-4">
          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Agent Information</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {[
                  { label: 'Type', value: 'Outbound Sales' },
                  { label: 'Industry', value: 'Technology' },
                  { label: 'Voice', value: 'Nova (Female, American)' },
                  { label: 'Language', value: 'English (US)' },
                  { label: 'Speaking Rate', value: '1.0x' },
                  { label: 'Max Duration', value: '5 minutes' },
                  { label: 'Created', value: 'Jan 15, 2024' },
                  { label: 'Last Modified', value: '2 days ago' },
                ].map((item) => (
                  <div key={item.label} className="flex items-center justify-between text-sm">
                    <span className="text-muted-foreground">{item.label}</span>
                    <span className="font-medium">{item.value}</span>
                  </div>
                ))}
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="text-base">System Prompt</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="rounded-md bg-muted/50 p-4 text-sm leading-relaxed">
                  You are a professional sales representative for TechCorp Solutions. Your goal
                  is to introduce our cloud platform to potential customers and schedule a demo
                  with our solutions team. Be friendly, professional, and concise. Always ask
                  permission before continuing the conversation. If the prospect is not interested,
                  thank them politely and end the call.
                </div>
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        <TabsContent value="configuration">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Configuration</CardTitle>
              <CardDescription>Current agent configuration and settings.</CardDescription>
            </CardHeader>
            <CardContent>
              <pre className="rounded-md bg-muted/50 p-4 text-sm overflow-auto max-h-96">
                {JSON.stringify(
                  {
                    name: 'Sales Outreach Pro',
                    type: 'outbound-sales',
                    industry: 'Technology',
                    voice: { id: 'nova', speakingRate: 1.0 },
                    language: 'en-US',
                    maxCallDuration: 300,
                    personality: 'Friendly, professional, patient',
                    tone: 'professional',
                    escalation: { enabled: true, threshold: 'negative_sentiment' },
                    compliance: { recordCalls: true, consentRequired: true },
                  },
                  null,
                  2
                )}
              </pre>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="calls">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Call History</CardTitle>
              <CardDescription>Recent calls made by this agent.</CardDescription>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground">
                Call history will load from the API. Use the Calls page for full filtering.
              </p>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="performance">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Performance Analytics</CardTitle>
              <CardDescription>Detailed performance metrics for this agent.</CardDescription>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground">
                Performance charts and analytics will be displayed here.
              </p>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="versions">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Version History</CardTitle>
              <CardDescription>Track changes to agent configuration over time.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              {[
                { version: 'v3', date: 'Mar 5, 2024', author: 'John Smith', change: 'Updated system prompt' },
                { version: 'v2', date: 'Feb 20, 2024', author: 'John Smith', change: 'Changed voice to Nova' },
                { version: 'v1', date: 'Jan 15, 2024', author: 'John Smith', change: 'Initial creation' },
              ].map((v) => (
                <div key={v.version} className="flex items-center justify-between rounded-lg border p-3">
                  <div className="flex items-center gap-3">
                    <Badge variant="outline">{v.version}</Badge>
                    <div>
                      <p className="text-sm font-medium">{v.change}</p>
                      <p className="text-xs text-muted-foreground">by {v.author}</p>
                    </div>
                  </div>
                  <span className="text-xs text-muted-foreground">{v.date}</span>
                </div>
              ))}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
