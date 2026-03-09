'use client';

import { CALL_STATUS_COLORS } from '@/lib/constants';
import { cn } from '@/lib/utils';

interface CallStatusBadgeProps {
  status: string;
}

export function CallStatusBadge({ status }: CallStatusBadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium',
        CALL_STATUS_COLORS[status] || 'bg-gray-100 text-gray-800'
      )}
    >
      {status === 'in-progress' && (
        <span className="mr-1.5 h-1.5 w-1.5 animate-pulse rounded-full bg-green-500" />
      )}
      {status.replace(/-/g, ' ')}
    </span>
  );
}
