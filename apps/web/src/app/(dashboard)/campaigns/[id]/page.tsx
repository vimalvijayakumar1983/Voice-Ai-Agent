'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { CampaignMetrics } from '@/components/campaigns/campaign-metrics';
import { Pause, Play, Settings, Download } from 'lucide-react';

export default function CampaignDetailPage({ params }: { params: { id: string } }) {
  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-3">
            <h2 className="text-2xl font-bold tracking-tight">Q1 Lead Generation</h2>
            <Badge variant="success">
              <span className="mr-1.5 h-1.5 w-1.5 animate-pulse rounded-full bg-green-500" />
              Running
            </Badge>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            Sales Outreach Pro &middot; Feb 1 - Mar 31, 2024
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm">
            <Pause className="mr-2 h-4 w-4" />
            Pause
          </Button>
          <Button variant="outline" size="sm">
            <Download className="mr-2 h-4 w-4" />
            Export
          </Button>
          <Button size="sm">
            <Settings className="mr-2 h-4 w-4" />
            Settings
          </Button>
        </div>
      </div>

      <CampaignMetrics
        totalCalls={5000}
        connectedCalls={2100}
        completedCalls={3400}
        failedCalls={180}
        avgDuration="3:28"
        conversionRate={24}
      />

      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="calls">Call List</TabsTrigger>
          <TabsTrigger value="outcomes">Outcomes</TabsTrigger>
          <TabsTrigger value="settings">Settings</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="space-y-4">
          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Campaign Progress</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="space-y-4">
                  <div>
                    <div className="mb-2 flex justify-between text-sm">
                      <span>Overall Progress</span>
                      <span className="font-medium">68%</span>
                    </div>
                    <div className="h-3 rounded-full bg-secondary">
                      <div className="h-full w-[68%] rounded-full bg-primary transition-all" />
                    </div>
                  </div>
                  <div className="grid grid-cols-2 gap-4 pt-2">
                    <div>
                      <p className="text-xs text-muted-foreground">Remaining</p>
                      <p className="text-lg font-bold">1,600</p>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground">Est. Completion</p>
                      <p className="text-lg font-bold">Mar 22</p>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground">Calls/Hour</p>
                      <p className="text-lg font-bold">142</p>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground">Active Channels</p>
                      <p className="text-lg font-bold">5</p>
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Outcome Breakdown</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="space-y-3">
                  {[
                    { label: 'Interested', count: 504, pct: 24, color: 'bg-green-500' },
                    { label: 'Not Interested', count: 420, pct: 20, color: 'bg-red-500' },
                    { label: 'Callback Requested', count: 315, pct: 15, color: 'bg-yellow-500' },
                    { label: 'No Answer', count: 672, pct: 32, color: 'bg-gray-400' },
                    { label: 'Voicemail', count: 189, pct: 9, color: 'bg-purple-500' },
                  ].map((item) => (
                    <div key={item.label} className="space-y-1">
                      <div className="flex justify-between text-sm">
                        <span>{item.label}</span>
                        <span className="text-muted-foreground">{item.count} ({item.pct}%)</span>
                      </div>
                      <div className="h-2 rounded-full bg-secondary">
                        <div
                          className={`h-full rounded-full ${item.color}`}
                          style={{ width: `${item.pct}%` }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        <TabsContent value="calls">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Call List</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground">
                All calls for this campaign will be listed here with status, outcome, and duration.
              </p>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="outcomes">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Detailed Outcomes</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground">
                Detailed outcome analytics and charts will display here.
              </p>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="settings">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Campaign Settings</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground">
                Campaign configuration including pacing, schedule, and retry settings.
              </p>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
