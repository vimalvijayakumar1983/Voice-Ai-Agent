'use client';

import { AgentForm } from '@/components/agents/agent-form';

export default function NewAgentPage() {
  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">Create New Agent</h2>
        <p className="text-sm text-muted-foreground">
          Configure your AI voice agent step by step
        </p>
      </div>
      <AgentForm />
    </div>
  );
}
