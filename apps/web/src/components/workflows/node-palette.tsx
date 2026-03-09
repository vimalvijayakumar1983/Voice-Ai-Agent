'use client';

import { useState, useCallback } from 'react';
import {
  MessageSquare,
  ShieldCheck,
  UserCheck,
  ClipboardCheck,
  FileText,
  Search,
  ListChecks,
  Menu,
  CalendarCheck,
  CreditCard,
  PhoneForwarded,
  Send,
  PhoneOff,
  Clock,
  GitBranch,
  Pause,
  Globe,
  Variable,
  SearchIcon,
} from 'lucide-react';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import { WorkflowNodeType } from '@/stores/workflow-store';

// ============================================================================
// Node Type Definitions
// ============================================================================

interface NodeTypeDefinition {
  type: WorkflowNodeType;
  label: string;
  description: string;
  icon: React.ElementType;
  category: 'conversation' | 'data' | 'action' | 'control';
  color: string;
}

const NODE_TYPES: NodeTypeDefinition[] = [
  // Conversation
  {
    type: WorkflowNodeType.GREETING,
    label: 'Greeting',
    description: 'Play greeting message, branch on time of day',
    icon: MessageSquare,
    category: 'conversation',
    color: 'text-green-600',
  },
  {
    type: WorkflowNodeType.CONSENT_CAPTURE,
    label: 'Consent Capture',
    description: 'Ask for consent and record response',
    icon: ShieldCheck,
    category: 'conversation',
    color: 'text-green-600',
  },
  {
    type: WorkflowNodeType.SUMMARIZE,
    label: 'Summarize',
    description: 'Summarize conversation so far',
    icon: ListChecks,
    category: 'conversation',
    color: 'text-green-600',
  },
  {
    type: WorkflowNodeType.OFFER_OPTIONS,
    label: 'Offer Options',
    description: 'Present a menu of options to choose from',
    icon: Menu,
    category: 'conversation',
    color: 'text-green-600',
  },

  // Data Collection
  {
    type: WorkflowNodeType.VERIFICATION,
    label: 'Verification',
    description: 'Verify identity (DOB, SSN, account number)',
    icon: UserCheck,
    category: 'data',
    color: 'text-blue-600',
  },
  {
    type: WorkflowNodeType.QUALIFICATION,
    label: 'Qualification',
    description: 'Ask qualifying questions and score lead',
    icon: ClipboardCheck,
    category: 'data',
    color: 'text-blue-600',
  },
  {
    type: WorkflowNodeType.INFO_CAPTURE,
    label: 'Info Capture',
    description: 'Collect specific information fields',
    icon: FileText,
    category: 'data',
    color: 'text-blue-600',
  },
  {
    type: WorkflowNodeType.LOOKUP,
    label: 'Lookup',
    description: 'Query external API or knowledge base',
    icon: Search,
    category: 'data',
    color: 'text-blue-600',
  },

  // Actions
  {
    type: WorkflowNodeType.BOOK_APPOINTMENT,
    label: 'Book Appointment',
    description: 'Check availability and book via API',
    icon: CalendarCheck,
    category: 'action',
    color: 'text-orange-600',
  },
  {
    type: WorkflowNodeType.COLLECT_PAYMENT,
    label: 'Collect Payment',
    description: 'PCI-compliant payment collection',
    icon: CreditCard,
    category: 'action',
    color: 'text-orange-600',
  },
  {
    type: WorkflowNodeType.ESCALATE_HUMAN,
    label: 'Escalate to Human',
    description: 'Transfer to a human agent',
    icon: PhoneForwarded,
    category: 'action',
    color: 'text-orange-600',
  },
  {
    type: WorkflowNodeType.SEND_MESSAGE,
    label: 'Send Message',
    description: 'Send SMS or email during call',
    icon: Send,
    category: 'action',
    color: 'text-orange-600',
  },
  {
    type: WorkflowNodeType.API_CALL,
    label: 'API Call',
    description: 'Make external API request',
    icon: Globe,
    category: 'action',
    color: 'text-orange-600',
  },

  // Control Flow
  {
    type: WorkflowNodeType.END_CALL,
    label: 'End Call',
    description: 'End with closing message',
    icon: PhoneOff,
    category: 'control',
    color: 'text-purple-600',
  },
  {
    type: WorkflowNodeType.RETRY_LATER,
    label: 'Retry Later',
    description: 'Schedule a callback',
    icon: Clock,
    category: 'control',
    color: 'text-purple-600',
  },
  {
    type: WorkflowNodeType.CONDITIONAL,
    label: 'Conditional',
    description: 'Evaluate conditions and branch',
    icon: GitBranch,
    category: 'control',
    color: 'text-purple-600',
  },
  {
    type: WorkflowNodeType.WAIT,
    label: 'Wait',
    description: 'Wait for input or event',
    icon: Pause,
    category: 'control',
    color: 'text-purple-600',
  },
  {
    type: WorkflowNodeType.SET_VARIABLE,
    label: 'Set Variable',
    description: 'Set a workflow variable value',
    icon: Variable,
    category: 'control',
    color: 'text-purple-600',
  },
];

const CATEGORY_LABELS: Record<string, string> = {
  conversation: 'Conversation',
  data: 'Data Collection',
  action: 'Actions',
  control: 'Control Flow',
};

const CATEGORY_COLORS: Record<string, string> = {
  conversation: 'border-green-200 dark:border-green-800',
  data: 'border-blue-200 dark:border-blue-800',
  action: 'border-orange-200 dark:border-orange-800',
  control: 'border-purple-200 dark:border-purple-800',
};

// ============================================================================
// Component
// ============================================================================

export function NodePalette() {
  const [search, setSearch] = useState('');

  const filteredTypes = NODE_TYPES.filter(
    (nt) =>
      nt.label.toLowerCase().includes(search.toLowerCase()) ||
      nt.description.toLowerCase().includes(search.toLowerCase()) ||
      nt.category.toLowerCase().includes(search.toLowerCase()),
  );

  const groupedTypes = filteredTypes.reduce(
    (acc, nt) => {
      if (!acc[nt.category]) acc[nt.category] = [];
      acc[nt.category].push(nt);
      return acc;
    },
    {} as Record<string, NodeTypeDefinition[]>,
  );

  const onDragStart = useCallback(
    (event: React.DragEvent, nodeType: NodeTypeDefinition) => {
      event.dataTransfer.setData(
        'application/workflow-node',
        JSON.stringify({
          type: nodeType.type,
          label: nodeType.label,
        }),
      );
      event.dataTransfer.effectAllowed = 'move';
    },
    [],
  );

  return (
    <div className="w-64 border-r bg-card flex flex-col h-full">
      <div className="p-3 border-b">
        <h3 className="text-sm font-semibold mb-2">Node Types</h3>
        <div className="relative">
          <SearchIcon className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
          <Input
            placeholder="Search nodes..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-8 h-8 text-sm"
          />
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-2 space-y-4">
        {Object.entries(groupedTypes).map(([category, types]) => (
          <div key={category}>
            <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-1.5 px-1">
              {CATEGORY_LABELS[category] || category}
            </h4>
            <div className="space-y-1">
              {types.map((nodeType) => {
                const Icon = nodeType.icon;
                return (
                  <div
                    key={nodeType.type}
                    draggable
                    onDragStart={(e) => onDragStart(e, nodeType)}
                    className={cn(
                      'flex items-center gap-2.5 px-2.5 py-2 rounded-md border cursor-grab active:cursor-grabbing',
                      'hover:bg-accent/50 transition-colors',
                      CATEGORY_COLORS[category],
                    )}
                    title={nodeType.description}
                  >
                    <Icon className={cn('w-4 h-4 shrink-0', nodeType.color)} />
                    <div className="min-w-0 flex-1">
                      <p className="text-xs font-medium truncate">
                        {nodeType.label}
                      </p>
                      <p className="text-[10px] text-muted-foreground truncate">
                        {nodeType.description}
                      </p>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}

        {filteredTypes.length === 0 && (
          <p className="text-sm text-muted-foreground text-center py-8">
            No matching node types
          </p>
        )}
      </div>
    </div>
  );
}
