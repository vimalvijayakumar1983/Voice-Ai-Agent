'use client';

import { useCallback } from 'react';
import { Play, Square, Eye, Trash2, FlaskConical, CheckCircle2, Clock, MoreHorizontal } from 'lucide-react';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { cn } from '@/lib/utils';
import {
  useExperiments,
  useStartExperiment,
  useStopExperiment,
  useDeleteExperiment,
  type Experiment,
} from '@/hooks/use-ab-testing';

// ============================================================================
// Status Badge
// ============================================================================

function StatusBadge({ status }: { status: string }) {
  const config: Record<string, { label: string; variant: 'default' | 'secondary' | 'destructive' | 'outline'; className: string }> = {
    DRAFT: { label: 'Draft', variant: 'secondary', className: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300' },
    RUNNING: { label: 'Running', variant: 'default', className: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400' },
    COMPLETED: { label: 'Completed', variant: 'secondary', className: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400' },
    APPLIED: { label: 'Applied', variant: 'secondary', className: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400' },
  };

  const c = config[status] || config.DRAFT;

  return (
    <Badge variant={c.variant} className={cn('text-[10px]', c.className)}>
      {c.label}
    </Badge>
  );
}

// ============================================================================
// Component
// ============================================================================

interface ExperimentListProps {
  onViewExperiment: (id: string) => void;
}

export function ExperimentList({ onViewExperiment }: ExperimentListProps) {
  const { data, isLoading } = useExperiments();
  const experiments = data?.experiments || [];

  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Experiment</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Variants</TableHead>
            <TableHead>Split Method</TableHead>
            <TableHead>Success Criteria</TableHead>
            <TableHead>Min Samples</TableHead>
            <TableHead>Created</TableHead>
            <TableHead className="w-10"></TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {isLoading && (
            <TableRow>
              <TableCell colSpan={8} className="text-center text-muted-foreground py-8">
                Loading experiments...
              </TableCell>
            </TableRow>
          )}
          {!isLoading && experiments.length === 0 && (
            <TableRow>
              <TableCell colSpan={8} className="text-center text-muted-foreground py-8">
                <FlaskConical className="w-8 h-8 mx-auto mb-2 opacity-50" />
                No experiments yet. Create one to get started.
              </TableCell>
            </TableRow>
          )}
          {experiments.map((exp) => (
            <ExperimentRow
              key={exp.id}
              experiment={exp}
              onView={() => onViewExperiment(exp.id)}
            />
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

// ============================================================================
// Row Component
// ============================================================================

function ExperimentRow({
  experiment,
  onView,
}: {
  experiment: Experiment;
  onView: () => void;
}) {
  const startMutation = useStartExperiment(experiment.id);
  const stopMutation = useStopExperiment(experiment.id);
  const deleteMutation = useDeleteExperiment(experiment.id);

  const handleStart = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      startMutation.mutate(undefined as any);
    },
    [startMutation],
  );

  const handleStop = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      stopMutation.mutate(undefined as any);
    },
    [stopMutation],
  );

  const handleDelete = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      if (confirm('Are you sure you want to delete this experiment?')) {
        deleteMutation.mutate(undefined as any);
      }
    },
    [deleteMutation],
  );

  return (
    <TableRow className="cursor-pointer hover:bg-muted/50" onClick={onView}>
      <TableCell>
        <div>
          <p className="font-medium text-sm">{experiment.name}</p>
          {experiment.description && (
            <p className="text-xs text-muted-foreground line-clamp-1">
              {experiment.description}
            </p>
          )}
        </div>
      </TableCell>
      <TableCell>
        <StatusBadge status={experiment.status} />
      </TableCell>
      <TableCell>
        <div className="flex items-center gap-1">
          {experiment.variants.map((v) => (
            <Badge
              key={v.id}
              variant="outline"
              className={cn(
                'text-[9px] px-1.5',
                v.isControl && 'border-primary text-primary',
              )}
            >
              {v.name} ({v.trafficPercentage}%)
            </Badge>
          ))}
        </div>
      </TableCell>
      <TableCell className="text-sm capitalize">
        {experiment.trafficSplitMethod.toLowerCase().replace(/_/g, ' ')}
      </TableCell>
      <TableCell className="text-sm capitalize">
        {experiment.successCriteria.replace(/_/g, ' ')}
      </TableCell>
      <TableCell className="text-sm">
        {experiment.minimumSampleSize}
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {new Date(experiment.createdAt).toLocaleDateString()}
      </TableCell>
      <TableCell>
        <DropdownMenu>
          <DropdownMenuTrigger asChild onClick={(e) => e.stopPropagation()}>
            <Button variant="ghost" size="sm" className="h-7 w-7 p-0">
              <MoreHorizontal className="w-3.5 h-3.5" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={onView}>
              <Eye className="w-3.5 h-3.5 mr-2" />
              View Results
            </DropdownMenuItem>
            {experiment.status === 'DRAFT' && (
              <DropdownMenuItem onClick={handleStart}>
                <Play className="w-3.5 h-3.5 mr-2" />
                Start
              </DropdownMenuItem>
            )}
            {experiment.status === 'RUNNING' && (
              <DropdownMenuItem onClick={handleStop}>
                <Square className="w-3.5 h-3.5 mr-2" />
                Stop
              </DropdownMenuItem>
            )}
            {experiment.status !== 'RUNNING' && (
              <DropdownMenuItem onClick={handleDelete} className="text-red-600">
                <Trash2 className="w-3.5 h-3.5 mr-2" />
                Delete
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </TableCell>
    </TableRow>
  );
}
