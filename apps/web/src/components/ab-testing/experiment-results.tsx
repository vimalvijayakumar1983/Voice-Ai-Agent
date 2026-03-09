'use client';

import { useMemo } from 'react';
import {
  Trophy,
  CheckCircle2,
  AlertTriangle,
  TrendingUp,
  BarChart3,
  Zap,
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { cn } from '@/lib/utils';
import {
  useExperiment,
  useExperimentResults,
  useApplyWinner,
  useStopExperiment,
  type VariantResult,
} from '@/hooks/use-ab-testing';

// ============================================================================
// Confidence Bar
// ============================================================================

function ConfidenceBar({ level }: { level: number }) {
  const percentage = Math.round(level * 100);
  const color =
    percentage >= 95
      ? 'bg-green-500'
      : percentage >= 90
        ? 'bg-yellow-500'
        : 'bg-red-500';

  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs">
        <span className="text-muted-foreground">Confidence</span>
        <span className="font-medium">{percentage}%</span>
      </div>
      <div className="h-2 bg-gray-100 dark:bg-gray-800 rounded-full overflow-hidden">
        <div
          className={cn('h-full rounded-full transition-all duration-500', color)}
          style={{ width: `${percentage}%` }}
        />
      </div>
      <div className="flex justify-between text-[10px] text-muted-foreground">
        <span>0%</span>
        <span className="border-l border-dashed border-gray-300 dark:border-gray-600 px-1">
          95% threshold
        </span>
        <span>100%</span>
      </div>
    </div>
  );
}

// ============================================================================
// Metric Comparison Bar
// ============================================================================

function ComparisonBar({
  variants,
  metricKey,
  label,
  format,
  higherIsBetter = true,
}: {
  variants: VariantResult[];
  metricKey: keyof VariantResult;
  label: string;
  format: (v: number) => string;
  higherIsBetter?: boolean;
}) {
  const values = variants.map((v) => Number(v[metricKey]) || 0);
  const maxVal = Math.max(...values, 0.01);
  const bestIndex = higherIsBetter
    ? values.indexOf(Math.max(...values))
    : values.indexOf(Math.min(...values));

  return (
    <div className="space-y-2">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      {variants.map((v, i) => (
        <div key={v.variantId} className="flex items-center gap-3">
          <span className="text-xs w-24 truncate">{v.name}</span>
          <div className="flex-1 h-6 bg-gray-100 dark:bg-gray-800 rounded-md overflow-hidden relative">
            <div
              className={cn(
                'h-full rounded-md transition-all duration-500',
                i === bestIndex
                  ? 'bg-primary'
                  : 'bg-primary/40',
              )}
              style={{
                width: `${(values[i] / maxVal) * 100}%`,
              }}
            />
          </div>
          <span
            className={cn(
              'text-xs font-medium w-16 text-right',
              i === bestIndex && 'text-primary font-bold',
            )}
          >
            {format(values[i])}
            {i === bestIndex && ' *'}
          </span>
        </div>
      ))}
    </div>
  );
}

// ============================================================================
// Component
// ============================================================================

interface ExperimentResultsProps {
  experimentId: string;
}

export function ExperimentResults({ experimentId }: ExperimentResultsProps) {
  const { data: experiment, isLoading: expLoading } = useExperiment(experimentId);
  const { data: results, isLoading: resLoading } = useExperimentResults(
    experimentId,
    experiment?.status === 'RUNNING',
  );
  const applyWinnerMutation = useApplyWinner(experimentId);
  const stopMutation = useStopExperiment(experimentId);

  const winner = useMemo(() => {
    if (!results?.recommendedWinner || !results.variants) return null;
    return results.variants.find((v) => v.variantId === results.recommendedWinner);
  }, [results]);

  if (expLoading || resLoading || !experiment) {
    return (
      <div className="flex items-center justify-center py-12 text-muted-foreground">
        Loading results...
      </div>
    );
  }

  if (!results) {
    return (
      <div className="text-center py-12 text-muted-foreground">
        <BarChart3 className="w-10 h-10 mx-auto mb-3 opacity-50" />
        <p>No results available yet. Start the experiment to begin collecting data.</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Status Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">{experiment.name}</h2>
          <p className="text-sm text-muted-foreground">
            {results.totalSamples} total samples collected
          </p>
        </div>
        <div className="flex items-center gap-2">
          {experiment.status === 'RUNNING' && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => stopMutation.mutate(undefined as any)}
              disabled={stopMutation.isPending}
            >
              Stop Experiment
            </Button>
          )}
          {experiment.status === 'COMPLETED' && winner && (
            <Button
              size="sm"
              onClick={() => applyWinnerMutation.mutate(undefined as any)}
              disabled={applyWinnerMutation.isPending}
            >
              <Trophy className="w-3.5 h-3.5 mr-1" />
              Apply Winner
            </Button>
          )}
        </div>
      </div>

      {/* Winner Banner */}
      {results.statisticalSignificance && winner && (
        <Card className="border-green-200 dark:border-green-800 bg-green-50 dark:bg-green-950/20">
          <CardContent className="p-4 flex items-center gap-3">
            <div className="p-2 rounded-full bg-green-100 dark:bg-green-900/30">
              <Trophy className="w-5 h-5 text-green-600" />
            </div>
            <div>
              <p className="font-semibold text-green-800 dark:text-green-300">
                Winner: {winner.name}
              </p>
              <p className="text-sm text-green-700 dark:text-green-400">
                Statistical significance reached with {(results.confidenceLevel * 100).toFixed(1)}% confidence.
              </p>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Not significant yet */}
      {!results.statisticalSignificance && results.totalSamples > 0 && (
        <Card className="border-yellow-200 dark:border-yellow-800 bg-yellow-50 dark:bg-yellow-950/20">
          <CardContent className="p-4 flex items-center gap-3">
            <div className="p-2 rounded-full bg-yellow-100 dark:bg-yellow-900/30">
              <AlertTriangle className="w-5 h-5 text-yellow-600" />
            </div>
            <div>
              <p className="font-semibold text-yellow-800 dark:text-yellow-300">
                Not yet statistically significant
              </p>
              <p className="text-sm text-yellow-700 dark:text-yellow-400">
                {results.minimumSampleReached
                  ? 'Minimum samples reached but results are not yet conclusive. Continue running.'
                  : `Need more samples. Minimum: ${experiment.minimumSampleSize} per variant.`}
              </p>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Confidence */}
      <Card>
        <CardContent className="p-4">
          <ConfidenceBar level={results.confidenceLevel} />
        </CardContent>
      </Card>

      {/* Metrics Table */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Variant Comparison</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Variant</TableHead>
                <TableHead>Samples</TableHead>
                <TableHead>Success Rate</TableHead>
                <TableHead>Conversion Rate</TableHead>
                <TableHead>Avg Duration</TableHead>
                <TableHead>Avg Sentiment</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {results.variants.map((v) => {
                const isWinner = v.variantId === results.recommendedWinner;
                return (
                  <TableRow key={v.variantId} className={cn(isWinner && 'bg-green-50/50 dark:bg-green-950/10')}>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <span className="font-medium">{v.name}</span>
                        {v.isControl && (
                          <Badge variant="outline" className="text-[9px]">
                            Control
                          </Badge>
                        )}
                        {isWinner && (
                          <Trophy className="w-3.5 h-3.5 text-yellow-500" />
                        )}
                      </div>
                    </TableCell>
                    <TableCell>{v.sampleSize}</TableCell>
                    <TableCell>
                      <span
                        className={cn(
                          'font-medium',
                          v.successRate >= 80 ? 'text-green-600' : v.successRate >= 60 ? 'text-yellow-600' : 'text-red-600',
                        )}
                      >
                        {v.successRate.toFixed(1)}%
                      </span>
                    </TableCell>
                    <TableCell>{v.conversionRate.toFixed(1)}%</TableCell>
                    <TableCell>{v.avgDuration.toFixed(0)}s</TableCell>
                    <TableCell>{v.avgSentiment.toFixed(2)}</TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {/* Visual Comparisons */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <Card>
          <CardContent className="p-4">
            <ComparisonBar
              variants={results.variants}
              metricKey="successRate"
              label="Success Rate"
              format={(v) => `${v.toFixed(1)}%`}
            />
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <ComparisonBar
              variants={results.variants}
              metricKey="conversionRate"
              label="Conversion Rate"
              format={(v) => `${v.toFixed(1)}%`}
            />
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <ComparisonBar
              variants={results.variants}
              metricKey="avgDuration"
              label="Average Duration"
              format={(v) => `${v.toFixed(0)}s`}
              higherIsBetter={false}
            />
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <ComparisonBar
              variants={results.variants}
              metricKey="avgSentiment"
              label="Average Sentiment"
              format={(v) => v.toFixed(2)}
            />
          </CardContent>
        </Card>
      </div>

      {/* Statistical Details */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Statistical Details</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div>
              <p className="text-muted-foreground text-xs">p-value</p>
              <p className="font-semibold">{results.pValue.toFixed(4)}</p>
            </div>
            <div>
              <p className="text-muted-foreground text-xs">Confidence Level</p>
              <p className="font-semibold">{(results.confidenceLevel * 100).toFixed(1)}%</p>
            </div>
            <div>
              <p className="text-muted-foreground text-xs">Total Samples</p>
              <p className="font-semibold">{results.totalSamples}</p>
            </div>
            <div>
              <p className="text-muted-foreground text-xs">Significant</p>
              <p className="font-semibold flex items-center gap-1">
                {results.statisticalSignificance ? (
                  <>
                    <CheckCircle2 className="w-3.5 h-3.5 text-green-600" /> Yes
                  </>
                ) : (
                  <>
                    <AlertTriangle className="w-3.5 h-3.5 text-yellow-600" /> Not Yet
                  </>
                )}
              </p>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
