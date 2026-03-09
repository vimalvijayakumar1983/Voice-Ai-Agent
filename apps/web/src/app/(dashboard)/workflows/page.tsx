'use client';

import Link from 'next/link';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Plus, GitBranch, Play, Pause, MoreVertical } from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

const mockWorkflows = [
  { id: '1', name: 'Sales Follow-up Flow', status: 'active', nodes: 8, triggers: 2, lastRun: '2 hours ago', successRate: 85 },
  { id: '2', name: 'Appointment Confirmation', status: 'active', nodes: 5, triggers: 1, lastRun: '30 min ago', successRate: 94 },
  { id: '3', name: 'Lead Scoring Pipeline', status: 'draft', nodes: 12, triggers: 3, lastRun: 'Never', successRate: 0 },
  { id: '4', name: 'Escalation Handler', status: 'active', nodes: 6, triggers: 1, lastRun: '1 hour ago', successRate: 78 },
  { id: '5', name: 'Post-Call Survey', status: 'inactive', nodes: 4, triggers: 1, lastRun: '3 days ago', successRate: 72 },
];

const statusVariant: Record<string, 'success' | 'secondary' | 'warning'> = {
  active: 'success',
  inactive: 'secondary',
  draft: 'warning',
};

export default function WorkflowsPage() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">Workflows</h2>
          <p className="text-sm text-muted-foreground">
            Automate call flows and post-call actions
          </p>
        </div>
        <Button asChild>
          <Link href="/workflows/new">
            <Plus className="mr-2 h-4 w-4" />
            Create Workflow
          </Link>
        </Button>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {mockWorkflows.map((wf) => (
          <Link key={wf.id} href={`/workflows/${wf.id}`}>
            <Card className="group transition-all hover:shadow-md hover:border-primary/20">
              <CardContent className="p-5">
                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-3">
                    <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
                      <GitBranch className="h-5 w-5 text-primary" />
                    </div>
                    <div>
                      <h3 className="font-semibold group-hover:text-primary transition-colors">
                        {wf.name}
                      </h3>
                      <p className="text-xs text-muted-foreground">
                        {wf.nodes} nodes &middot; {wf.triggers} triggers
                      </p>
                    </div>
                  </div>
                  <Badge variant={statusVariant[wf.status]}>{wf.status}</Badge>
                </div>
                <div className="mt-4 flex items-center justify-between text-xs text-muted-foreground">
                  <span>Last run: {wf.lastRun}</span>
                  {wf.successRate > 0 && <span>{wf.successRate}% success</span>}
                </div>
              </CardContent>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}
