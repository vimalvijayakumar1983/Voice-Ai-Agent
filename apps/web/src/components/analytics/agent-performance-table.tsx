'use client';

import { useState, useMemo } from 'react';
import { TrendingUp, TrendingDown, ArrowUpDown, ChevronRight } from 'lucide-react';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { useApiQuery } from '@/hooks/use-api';

// ============================================================================
// Types
// ============================================================================

interface AgentScore {
  agentId: string;
  agentName: string;
  totalCalls: number;
  completedCalls: number;
  successRate: number;
  avgDuration: number;
  avgSentiment: number;
  score: number;
  rank: number;
  trend: number;
}

interface AgentPerformanceData {
  period: { from: string; to: string };
  agents: AgentScore[];
  summary: {
    totalAgents: number;
    avgScore: number;
    topPerformer: AgentScore | null;
  };
}

type SortKey = 'rank' | 'agentName' | 'totalCalls' | 'successRate' | 'avgDuration' | 'avgSentiment' | 'score';

// ============================================================================
// Component
// ============================================================================

export function AgentPerformanceTable() {
  const [sortKey, setSortKey] = useState<SortKey>('rank');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc');

  const from = useMemo(() => {
    const d = new Date();
    d.setDate(d.getDate() - 30);
    return d.toISOString();
  }, []);

  const { data, isLoading } = useApiQuery<AgentPerformanceData>(
    ['analytics', 'agent-performance'],
    `/analytics/agents/performance?from=${from}`,
  );

  const agents = useMemo(() => {
    const list = data?.agents || [];
    return [...list].sort((a, b) => {
      const aVal = a[sortKey];
      const bVal = b[sortKey];
      const dir = sortDir === 'asc' ? 1 : -1;
      if (typeof aVal === 'string' && typeof bVal === 'string') {
        return aVal.localeCompare(bVal) * dir;
      }
      return ((aVal as number) - (bVal as number)) * dir;
    });
  }, [data?.agents, sortKey, sortDir]);

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  };

  const SortHeader = ({ label, field }: { label: string; field: SortKey }) => (
    <TableHead>
      <button
        onClick={() => handleSort(field)}
        className="flex items-center gap-1 hover:text-foreground transition-colors"
      >
        {label}
        <ArrowUpDown className="w-3 h-3" />
      </button>
    </TableHead>
  );

  const formatDuration = (seconds: number) => {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  };

  const getSentimentLabel = (score: number) => {
    if (score > 0.3) return { label: 'Positive', color: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400' };
    if (score < -0.3) return { label: 'Negative', color: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400' };
    return { label: 'Neutral', color: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-400' };
  };

  // Mini sparkline bar
  const SparkBar = ({ value, max }: { value: number; max: number }) => (
    <div className="w-16 h-2 bg-gray-100 dark:bg-gray-800 rounded-full overflow-hidden">
      <div
        className="h-full bg-primary rounded-full transition-all"
        style={{ width: `${Math.min(100, (value / max) * 100)}%` }}
      />
    </div>
  );

  const maxCalls = Math.max(...agents.map((a) => a.totalCalls), 1);

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-base font-semibold">Agent Performance</CardTitle>
        {data?.summary && (
          <div className="flex items-center gap-4 text-xs text-muted-foreground">
            <span>{data.summary.totalAgents} agents</span>
            <span>Avg Score: {data.summary.avgScore.toFixed(1)}</span>
          </div>
        )}
      </CardHeader>
      <CardContent className="p-0">
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <SortHeader label="#" field="rank" />
                <SortHeader label="Agent" field="agentName" />
                <SortHeader label="Total Calls" field="totalCalls" />
                <SortHeader label="Success Rate" field="successRate" />
                <SortHeader label="Avg Duration" field="avgDuration" />
                <SortHeader label="Sentiment" field="avgSentiment" />
                <SortHeader label="Score" field="score" />
                <TableHead>Trend</TableHead>
                <TableHead></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {agents.length === 0 && (
                <TableRow>
                  <TableCell colSpan={9} className="text-center text-muted-foreground py-8">
                    {isLoading ? 'Loading...' : 'No agent data available'}
                  </TableCell>
                </TableRow>
              )}
              {agents.map((agent) => {
                const sentiment = getSentimentLabel(agent.avgSentiment);
                return (
                  <TableRow key={agent.agentId} className="hover:bg-muted/50">
                    <TableCell className="font-medium text-muted-foreground w-10">
                      {agent.rank}
                    </TableCell>
                    <TableCell>
                      <span className="font-medium">{agent.agentName}</span>
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <span className="text-sm">{agent.totalCalls}</span>
                        <SparkBar value={agent.totalCalls} max={maxCalls} />
                      </div>
                    </TableCell>
                    <TableCell>
                      <span
                        className={cn(
                          'text-sm font-medium',
                          agent.successRate >= 80
                            ? 'text-green-600'
                            : agent.successRate >= 60
                              ? 'text-yellow-600'
                              : 'text-red-600',
                        )}
                      >
                        {agent.successRate.toFixed(1)}%
                      </span>
                    </TableCell>
                    <TableCell className="text-sm">
                      {formatDuration(agent.avgDuration)}
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary" className={cn('text-[10px]', sentiment.color)}>
                        {sentiment.label}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <span className="text-sm font-semibold">{agent.score.toFixed(1)}</span>
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1">
                        {agent.trend >= 0 ? (
                          <TrendingUp className="w-3.5 h-3.5 text-green-600" />
                        ) : (
                          <TrendingDown className="w-3.5 h-3.5 text-red-600" />
                        )}
                        <span
                          className={cn(
                            'text-xs font-medium',
                            agent.trend >= 0 ? 'text-green-600' : 'text-red-600',
                          )}
                        >
                          {agent.trend >= 0 ? '+' : ''}
                          {agent.trend.toFixed(1)}%
                        </span>
                      </div>
                    </TableCell>
                    <TableCell>
                      <Button variant="ghost" size="sm" className="h-7 w-7 p-0">
                        <ChevronRight className="w-3.5 h-3.5" />
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}
