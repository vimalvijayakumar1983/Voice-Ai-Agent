'use client';

import { memo, useState, useCallback } from 'react';
import {
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  type EdgeProps,
} from 'reactflow';
import { X } from 'lucide-react';
import { cn } from '@/lib/utils';

interface WorkflowEdgeData {
  conditions?: {
    expression?: string;
    operator?: string;
    field?: string;
    value?: unknown;
  };
}

export const WorkflowEdgeComponent = memo(function WorkflowEdgeComponent({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  label,
  data,
  selected,
  style = {},
  markerEnd,
}: EdgeProps<WorkflowEdgeData>) {
  const [isHovered, setIsHovered] = useState(false);

  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });

  const handleDelete = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      window.dispatchEvent(
        new CustomEvent('workflow:delete-edge', { detail: { edgeId: id } }),
      );
    },
    [id],
  );

  const conditionLabel =
    label ||
    data?.conditions?.expression ||
    (data?.conditions?.operator && data?.conditions?.field
      ? `${data.conditions.field} ${data.conditions.operator} ${data.conditions.value ?? ''}`
      : null);

  return (
    <>
      {/* Invisible wider path for easier hover detection */}
      <path
        d={edgePath}
        fill="none"
        stroke="transparent"
        strokeWidth={20}
        onMouseEnter={() => setIsHovered(true)}
        onMouseLeave={() => setIsHovered(false)}
      />

      <BaseEdge
        path={edgePath}
        markerEnd={markerEnd}
        style={{
          stroke: selected
            ? 'hsl(var(--primary))'
            : isHovered
              ? '#6366f1'
              : '#94a3b8',
          strokeWidth: selected || isHovered ? 2.5 : 1.5,
          transition: 'stroke 0.15s, stroke-width 0.15s',
          ...style,
        }}
      />

      {/* Animated flow dots */}
      <circle r={3} fill="#6366f1" opacity={selected || isHovered ? 0.8 : 0}>
        <animateMotion dur="2s" repeatCount="indefinite" path={edgePath} />
      </circle>

      <EdgeLabelRenderer>
        {/* Condition label */}
        {conditionLabel && (
          <div
            style={{
              position: 'absolute',
              transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)`,
              pointerEvents: 'all',
            }}
            className={cn(
              'px-2 py-0.5 rounded-full text-[10px] font-medium border shadow-sm transition-all',
              selected
                ? 'bg-primary text-primary-foreground border-primary'
                : 'bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-300 border-gray-200 dark:border-gray-700',
            )}
            onMouseEnter={() => setIsHovered(true)}
            onMouseLeave={() => setIsHovered(false)}
          >
            {String(conditionLabel)}
          </div>
        )}

        {/* Delete button */}
        {isHovered && (
          <div
            style={{
              position: 'absolute',
              transform: `translate(-50%, -50%) translate(${labelX}px,${labelY - (conditionLabel ? 20 : 0)}px)`,
              pointerEvents: 'all',
            }}
          >
            <button
              onClick={handleDelete}
              className="w-5 h-5 rounded-full bg-red-500 text-white flex items-center justify-center hover:bg-red-600 transition-colors shadow-sm"
              onMouseEnter={() => setIsHovered(true)}
            >
              <X className="w-3 h-3" />
            </button>
          </div>
        )}
      </EdgeLabelRenderer>
    </>
  );
});

export default WorkflowEdgeComponent;
