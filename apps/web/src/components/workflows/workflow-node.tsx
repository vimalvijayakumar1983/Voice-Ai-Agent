'use client';

import { memo, useCallback, useState } from 'react';
import { Handle, Position, type NodeProps } from 'reactflow';
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
  X,
  GripVertical,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { WorkflowNodeType, type WorkflowNodeData } from '@/stores/workflow-store';

// ============================================================================
// Node Appearance Configuration
// ============================================================================

interface NodeAppearance {
  icon: React.ElementType;
  color: string;
  bgColor: string;
  borderColor: string;
  category: 'conversation' | 'data' | 'action' | 'control';
}

const NODE_APPEARANCE: Record<string, NodeAppearance> = {
  [WorkflowNodeType.GREETING]: {
    icon: MessageSquare,
    color: 'text-green-700 dark:text-green-400',
    bgColor: 'bg-green-50 dark:bg-green-950/40',
    borderColor: 'border-green-300 dark:border-green-700',
    category: 'conversation',
  },
  [WorkflowNodeType.CONSENT_CAPTURE]: {
    icon: ShieldCheck,
    color: 'text-green-700 dark:text-green-400',
    bgColor: 'bg-green-50 dark:bg-green-950/40',
    borderColor: 'border-green-300 dark:border-green-700',
    category: 'conversation',
  },
  [WorkflowNodeType.VERIFICATION]: {
    icon: UserCheck,
    color: 'text-blue-700 dark:text-blue-400',
    bgColor: 'bg-blue-50 dark:bg-blue-950/40',
    borderColor: 'border-blue-300 dark:border-blue-700',
    category: 'data',
  },
  [WorkflowNodeType.QUALIFICATION]: {
    icon: ClipboardCheck,
    color: 'text-blue-700 dark:text-blue-400',
    bgColor: 'bg-blue-50 dark:bg-blue-950/40',
    borderColor: 'border-blue-300 dark:border-blue-700',
    category: 'data',
  },
  [WorkflowNodeType.INFO_CAPTURE]: {
    icon: FileText,
    color: 'text-blue-700 dark:text-blue-400',
    bgColor: 'bg-blue-50 dark:bg-blue-950/40',
    borderColor: 'border-blue-300 dark:border-blue-700',
    category: 'data',
  },
  [WorkflowNodeType.LOOKUP]: {
    icon: Search,
    color: 'text-blue-700 dark:text-blue-400',
    bgColor: 'bg-blue-50 dark:bg-blue-950/40',
    borderColor: 'border-blue-300 dark:border-blue-700',
    category: 'data',
  },
  [WorkflowNodeType.SUMMARIZE]: {
    icon: ListChecks,
    color: 'text-green-700 dark:text-green-400',
    bgColor: 'bg-green-50 dark:bg-green-950/40',
    borderColor: 'border-green-300 dark:border-green-700',
    category: 'conversation',
  },
  [WorkflowNodeType.OFFER_OPTIONS]: {
    icon: Menu,
    color: 'text-green-700 dark:text-green-400',
    bgColor: 'bg-green-50 dark:bg-green-950/40',
    borderColor: 'border-green-300 dark:border-green-700',
    category: 'conversation',
  },
  [WorkflowNodeType.BOOK_APPOINTMENT]: {
    icon: CalendarCheck,
    color: 'text-orange-700 dark:text-orange-400',
    bgColor: 'bg-orange-50 dark:bg-orange-950/40',
    borderColor: 'border-orange-300 dark:border-orange-700',
    category: 'action',
  },
  [WorkflowNodeType.COLLECT_PAYMENT]: {
    icon: CreditCard,
    color: 'text-orange-700 dark:text-orange-400',
    bgColor: 'bg-orange-50 dark:bg-orange-950/40',
    borderColor: 'border-orange-300 dark:border-orange-700',
    category: 'action',
  },
  [WorkflowNodeType.ESCALATE_HUMAN]: {
    icon: PhoneForwarded,
    color: 'text-orange-700 dark:text-orange-400',
    bgColor: 'bg-orange-50 dark:bg-orange-950/40',
    borderColor: 'border-orange-300 dark:border-orange-700',
    category: 'action',
  },
  [WorkflowNodeType.SEND_MESSAGE]: {
    icon: Send,
    color: 'text-orange-700 dark:text-orange-400',
    bgColor: 'bg-orange-50 dark:bg-orange-950/40',
    borderColor: 'border-orange-300 dark:border-orange-700',
    category: 'action',
  },
  [WorkflowNodeType.END_CALL]: {
    icon: PhoneOff,
    color: 'text-red-700 dark:text-red-400',
    bgColor: 'bg-red-50 dark:bg-red-950/40',
    borderColor: 'border-red-300 dark:border-red-700',
    category: 'control',
  },
  [WorkflowNodeType.RETRY_LATER]: {
    icon: Clock,
    color: 'text-purple-700 dark:text-purple-400',
    bgColor: 'bg-purple-50 dark:bg-purple-950/40',
    borderColor: 'border-purple-300 dark:border-purple-700',
    category: 'control',
  },
  [WorkflowNodeType.CONDITIONAL]: {
    icon: GitBranch,
    color: 'text-purple-700 dark:text-purple-400',
    bgColor: 'bg-purple-50 dark:bg-purple-950/40',
    borderColor: 'border-purple-300 dark:border-purple-700',
    category: 'control',
  },
  [WorkflowNodeType.WAIT]: {
    icon: Pause,
    color: 'text-purple-700 dark:text-purple-400',
    bgColor: 'bg-purple-50 dark:bg-purple-950/40',
    borderColor: 'border-purple-300 dark:border-purple-700',
    category: 'control',
  },
  [WorkflowNodeType.API_CALL]: {
    icon: Globe,
    color: 'text-orange-700 dark:text-orange-400',
    bgColor: 'bg-orange-50 dark:bg-orange-950/40',
    borderColor: 'border-orange-300 dark:border-orange-700',
    category: 'action',
  },
  [WorkflowNodeType.SET_VARIABLE]: {
    icon: Variable,
    color: 'text-purple-700 dark:text-purple-400',
    bgColor: 'bg-purple-50 dark:bg-purple-950/40',
    borderColor: 'border-purple-300 dark:border-purple-700',
    category: 'control',
  },
};

const DEFAULT_APPEARANCE: NodeAppearance = {
  icon: MessageSquare,
  color: 'text-gray-700 dark:text-gray-400',
  bgColor: 'bg-gray-50 dark:bg-gray-950/40',
  borderColor: 'border-gray-300 dark:border-gray-700',
  category: 'conversation',
};

// ============================================================================
// Component
// ============================================================================

export const WorkflowNodeComponent = memo(function WorkflowNodeComponent({
  data,
  selected,
  id,
}: NodeProps<WorkflowNodeData>) {
  const [isHovered, setIsHovered] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);

  const appearance = NODE_APPEARANCE[data.type] || DEFAULT_APPEARANCE;
  const Icon = appearance.icon;

  const handleDelete = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      // Dispatch custom event for the canvas to handle
      window.dispatchEvent(
        new CustomEvent('workflow:delete-node', { detail: { nodeId: id } }),
      );
    },
    [id],
  );

  const handleDoubleClick = useCallback(() => {
    window.dispatchEvent(
      new CustomEvent('workflow:edit-node', { detail: { nodeId: id } }),
    );
  }, [id]);

  const statusColor = data._executionStatus
    ? data._executionStatus === 'success'
      ? 'bg-green-500'
      : data._executionStatus === 'failed'
        ? 'bg-red-500'
        : data._executionStatus === 'running'
          ? 'bg-yellow-500 animate-pulse'
          : 'bg-gray-400'
    : null;

  return (
    <div
      className={cn(
        'relative rounded-lg border-2 shadow-sm transition-all duration-150 min-w-[180px] max-w-[280px]',
        appearance.bgColor,
        appearance.borderColor,
        selected && 'ring-2 ring-primary ring-offset-2 shadow-md',
        isHovered && !selected && 'shadow-md',
      )}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      onDoubleClick={handleDoubleClick}
    >
      {/* Input Handle */}
      <Handle
        type="target"
        position={Position.Top}
        className="!w-3 !h-3 !bg-gray-400 !border-2 !border-white dark:!border-gray-900 !-top-1.5"
      />

      {/* Delete button */}
      {isHovered && (
        <button
          onClick={handleDelete}
          className="absolute -top-2 -right-2 z-10 w-5 h-5 rounded-full bg-red-500 text-white flex items-center justify-center hover:bg-red-600 transition-colors shadow-sm"
        >
          <X className="w-3 h-3" />
        </button>
      )}

      {/* Status indicator */}
      {statusColor && (
        <div
          className={cn(
            'absolute -top-1 -left-1 w-3 h-3 rounded-full border-2 border-white dark:border-gray-900',
            statusColor,
          )}
        />
      )}

      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2">
        <div className="cursor-grab active:cursor-grabbing">
          <GripVertical className="w-3.5 h-3.5 text-gray-400" />
        </div>
        <div className={cn('p-1.5 rounded-md', appearance.bgColor)}>
          <Icon className={cn('w-4 h-4', appearance.color)} />
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium truncate text-foreground">
            {data.label}
          </p>
          <p className="text-[10px] text-muted-foreground uppercase tracking-wider">
            {data.type.replace(/_/g, ' ')}
          </p>
        </div>
      </div>

      {/* Expanded content */}
      {(isExpanded || selected) && data.prompt && (
        <div className="px-3 pb-2 border-t border-gray-200 dark:border-gray-700 mt-1 pt-2">
          <p className="text-xs text-muted-foreground line-clamp-3">
            {data.prompt}
          </p>
        </div>
      )}

      {/* Compact prompt preview */}
      {!isExpanded && !selected && data.prompt && (
        <div className="px-3 pb-2">
          <p className="text-[11px] text-muted-foreground truncate">
            {data.prompt}
          </p>
        </div>
      )}

      {/* Output Handle */}
      <Handle
        type="source"
        position={Position.Bottom}
        className="!w-3 !h-3 !bg-gray-400 !border-2 !border-white dark:!border-gray-900 !-bottom-1.5"
      />

      {/* Multiple output handles for branching nodes */}
      {(data.type === WorkflowNodeType.CONDITIONAL ||
        data.type === WorkflowNodeType.CONSENT_CAPTURE ||
        data.type === WorkflowNodeType.VERIFICATION ||
        data.type === WorkflowNodeType.OFFER_OPTIONS) && (
        <>
          <Handle
            type="source"
            position={Position.Right}
            id="branch-right"
            className="!w-3 !h-3 !bg-green-500 !border-2 !border-white dark:!border-gray-900 !-right-1.5"
          />
          <Handle
            type="source"
            position={Position.Left}
            id="branch-left"
            className="!w-3 !h-3 !bg-red-500 !border-2 !border-white dark:!border-gray-900 !-left-1.5"
          />
        </>
      )}
    </div>
  );
});

export default WorkflowNodeComponent;
