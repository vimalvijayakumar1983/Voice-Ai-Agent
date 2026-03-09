'use client';

import { type LucideIcon, TrendingUp, TrendingDown } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { cn } from '@/lib/utils';

interface KpiCardProps {
  title: string;
  value: string;
  change?: number;
  changeLabel?: string;
  icon: LucideIcon;
  iconColor?: string;
  iconBg?: string;
}

export function KpiCard({
  title,
  value,
  change,
  changeLabel = 'vs last period',
  icon: Icon,
  iconColor = 'text-primary',
  iconBg = 'bg-primary/10',
}: KpiCardProps) {
  const isPositive = change !== undefined && change >= 0;

  return (
    <Card className="transition-shadow hover:shadow-md">
      <CardContent className="p-6">
        <div className="flex items-start justify-between">
          <div className="space-y-2">
            <p className="text-sm font-medium text-muted-foreground">{title}</p>
            <p className="text-3xl font-bold tracking-tight">{value}</p>
            {change !== undefined && (
              <div className="flex items-center gap-1.5">
                {isPositive ? (
                  <TrendingUp className="h-3.5 w-3.5 text-green-600 dark:text-green-400" />
                ) : (
                  <TrendingDown className="h-3.5 w-3.5 text-red-600 dark:text-red-400" />
                )}
                <span
                  className={cn(
                    'text-xs font-medium',
                    isPositive
                      ? 'text-green-600 dark:text-green-400'
                      : 'text-red-600 dark:text-red-400'
                  )}
                >
                  {isPositive ? '+' : ''}
                  {change.toFixed(1)}%
                </span>
                <span className="text-xs text-muted-foreground">{changeLabel}</span>
              </div>
            )}
          </div>
          <div className={cn('rounded-xl p-3', iconBg)}>
            <Icon className={cn('h-5 w-5', iconColor)} />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
