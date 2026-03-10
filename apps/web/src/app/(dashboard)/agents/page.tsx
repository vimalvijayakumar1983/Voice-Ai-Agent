'use client';

import { useState } from 'react';
import Link from 'next/link';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { AgentCard } from '@/components/agents/agent-card';
import { Plus, Search, Filter } from 'lucide-react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { DemoCallDialog } from '@/components/agents/demo-call-dialog';

const mockAgents = [
  { id: '1', name: 'Sales Outreach Pro', type: 'outbound-sales', industry: 'Technology', status: 'active' as const, callCount: 12450, successRate: 68, language: 'English (US)' },
  { id: '2', name: 'Customer Support Bot', type: 'inbound-support', industry: 'E-commerce', status: 'active' as const, callCount: 8920, successRate: 92, language: 'English (US)' },
  { id: '3', name: 'Appointment Scheduler', type: 'appointment-booking', industry: 'Healthcare', status: 'active' as const, callCount: 5340, successRate: 74, language: 'English (US)' },
  { id: '4', name: 'Lead Qualifier', type: 'lead-qualification', industry: 'Real Estate', status: 'inactive' as const, callCount: 3210, successRate: 61, language: 'English (US)' },
  { id: '5', name: 'Survey Agent', type: 'survey', industry: 'Insurance', status: 'draft' as const, callCount: 0, successRate: 0, language: 'Spanish' },
  { id: '6', name: 'Collections Agent', type: 'collections', industry: 'Financial Services', status: 'active' as const, callCount: 7650, successRate: 45, language: 'English (US)' },
];

export default function AgentsPage() {
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState('all');
  const [demoCallAgent, setDemoCallAgent] = useState<{ id: string; name: string } | null>(null);

  const filtered = mockAgents.filter((agent) => {
    const matchesSearch = agent.name.toLowerCase().includes(search.toLowerCase());
    const matchesType = typeFilter === 'all' || agent.type === typeFilter;
    return matchesSearch && matchesType;
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">AI Agents</h2>
          <p className="text-sm text-muted-foreground">
            Manage and configure your voice AI agents
          </p>
        </div>
        <Button asChild>
          <Link href="/agents/new">
            <Plus className="mr-2 h-4 w-4" />
            Create Agent
          </Link>
        </Button>
      </div>

      <div className="flex items-center gap-3">
        <div className="relative flex-1 max-w-sm">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search agents..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9"
          />
        </div>
        <Select value={typeFilter} onValueChange={setTypeFilter}>
          <SelectTrigger className="w-[200px]">
            <Filter className="mr-2 h-4 w-4" />
            <SelectValue placeholder="Filter by type" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Types</SelectItem>
            <SelectItem value="outbound-sales">Outbound Sales</SelectItem>
            <SelectItem value="inbound-support">Inbound Support</SelectItem>
            <SelectItem value="appointment-booking">Appointment Booking</SelectItem>
            <SelectItem value="lead-qualification">Lead Qualification</SelectItem>
            <SelectItem value="survey">Survey</SelectItem>
            <SelectItem value="collections">Collections</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {filtered.map((agent) => (
          <AgentCard
            key={agent.id}
            {...agent}
            onDemoCall={(agentId, agentName) => setDemoCallAgent({ id: agentId, name: agentName })}
          />
        ))}
      </div>

      {filtered.length === 0 && (
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <p className="text-lg font-medium text-muted-foreground">No agents found</p>
          <p className="mt-1 text-sm text-muted-foreground">
            Try adjusting your search or filters
          </p>
        </div>
      )}

      {/* Demo Call Dialog */}
      {demoCallAgent && (
        <DemoCallDialog
          open={!!demoCallAgent}
          onOpenChange={(open) => !open && setDemoCallAgent(null)}
          agentId={demoCallAgent.id}
          agentName={demoCallAgent.name}
        />
      )}
    </div>
  );
}
