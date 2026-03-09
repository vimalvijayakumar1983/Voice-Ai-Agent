'use client';

import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { GitBranch, Play, Pause, Settings, Clock, CheckCircle } from 'lucide-react';

export default function WorkflowDetailPage({ params }: { params: { id: string } }) {
  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-4">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-primary/10">
            <GitBranch className="h-6 w-6 text-primary" />
          </div>
          <div>
            <div className="flex items-center gap-3">
              <h2 className="text-2xl font-bold tracking-tight">Sales Follow-up Flow</h2>
              <Badge variant="success">Active</Badge>
            </div>
            <p className="text-sm text-muted-foreground">
              8 nodes &middot; 2 triggers &middot; Last run: 2 hours ago
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm">
            <Pause className="mr-2 h-4 w-4" />
            Pause
          </Button>
          <Button size="sm">
            <Settings className="mr-2 h-4 w-4" />
            Edit
          </Button>
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <Play className="h-5 w-5 text-green-500" />
            <div>
              <p className="text-xs text-muted-foreground">Total Executions</p>
              <p className="text-lg font-bold">1,245</p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <CheckCircle className="h-5 w-5 text-blue-500" />
            <div>
              <p className="text-xs text-muted-foreground">Success Rate</p>
              <p className="text-lg font-bold">85%</p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <Clock className="h-5 w-5 text-orange-500" />
            <div>
              <p className="text-xs text-muted-foreground">Avg Execution Time</p>
              <p className="text-lg font-bold">2.4s</p>
            </div>
          </CardContent>
        </Card>
      </div>

      <Tabs defaultValue="flow">
        <TabsList>
          <TabsTrigger value="flow">Flow</TabsTrigger>
          <TabsTrigger value="runs">Runs</TabsTrigger>
          <TabsTrigger value="settings">Settings</TabsTrigger>
        </TabsList>
        <TabsContent value="flow">
          <Card>
            <CardContent className="flex min-h-[400px] items-center justify-center">
              <p className="text-muted-foreground">Visual workflow representation</p>
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="runs">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Recent Runs</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground">Run history will display here.</p>
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="settings">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Workflow Settings</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground">Configuration options will display here.</p>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
